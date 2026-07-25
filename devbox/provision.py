"""Modular provisioning: composable, idempotent "mods".

A **mod** is one idempotent bash script that installs/configures one thing (jj,
dotfiles, Claude Code, a project's rust toolchain, ...). A project's effective
playbook is the built-in base list plus the project's own `mods` (appended),
each resolved by name with precedence **project > global > built-in**:

    mods/*.sh                     # built-in, at the repo root
    ~/.config/devbox/mods/*.sh    # global user (shared across projects)
    .devbox/mods/*.sh             # project-local (can shadow a built-in by name)

Mods are *static files* (lintable, shellcheck-able, diffable) that receive
context via the **environment** (DEVBOX_USER, DEVBOX_JJ_NAME, ...) rather than
`%TOKEN%` substitution; the GitHub PAT goes in a mode-0600 env file the driver
sources without tracing, so it never lands in a script on disk or a log.

One driver (`provision-driver.sh`) runs the resolved set in order. Provisioning
is delivered entirely over SSH (single path): devbox tars the payload
(`build_tarball`), pipes it to the box, extracts into a per-run temp dir owned by
the SSH user, and runs the driver - on `devbox create` (after boot) and on
`devbox sync --mods` alike (see cli.py). The init script only does base setup
(user/hostname/BASH_ENV/packages); it carries no provisioning payload.
"""
from __future__ import annotations

import dataclasses
import io
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

from .config import GLOBAL_CONFIG_PATH, GlobalConfig, ProjectConfig, PROJECT_MARKER_DIR
from .util import die

# The built-in mods (shipped in the package) and the standalone driver.
BUILTIN_MODS_DIR = Path(__file__).parent.parent / "mods"
DRIVER_PATH = Path(__file__).parent / "provision-driver.sh"
# Global user mods, shared across a user's projects.
GLOBAL_MODS_DIR = GLOBAL_CONFIG_PATH.parent / "mods"
# Project-local mods live under the marker dir: <marker>/.devbox/mods/*.sh.
PROJECT_MODS_SUBDIR = "mods"

# The default base playbook (built-ins, fixed order). Ordering constraints are
# encoded here: jj + github-auth must precede repo-bootstrap so a private
# `jj git clone` can authenticate. A project's `mods` are appended after this.
DEFAULT_BASE_MODS = ("jj", "github-auth", "dotfiles", "vim", "claude-code", "repo-bootstrap")

# Provisioning output log, under the dev user's $HOME - written (tee'd) when the
# driver runs over SSH, and tailed by `devbox status`. The driver additionally
# keeps a per-mod log at ~/provision/<name>.log.
PROVISION_LOG = "provision.log"


@dataclasses.dataclass
class PayloadFile:
    """One file in the provisioning payload, positioned relative to the payload
    root (the per-run temp dir the tarball is extracted into on the box)."""
    path: str          # relative path, e.g. "provision-driver.sh" or "mods/jj.sh"
    content: str
    mode: int          # unix permission bits (e.g. 0o755, 0o644)


# ---- host identity resolution (jj / GitHub) ---------------------------------
def _jj_config_get(key: str) -> Optional[str]:
    """Best-effort read of the host's own jj config (e.g. user.name/user.email)."""
    if not shutil.which("jj"):
        return None
    r = subprocess.run(["jj", "config", "get", key], capture_output=True, text=True)
    value = r.stdout.strip()
    return value if r.returncode == 0 and value else None


def resolve_jj_identity(gc: GlobalConfig) -> tuple:
    """[jj] name/email from global config, falling back to the host's own
    `jj config get user.name`/`user.email` if unset."""
    name = gc.jj_name or _jj_config_get("user.name")
    email = gc.jj_email or _jj_config_get("user.email")
    if name is None or email is None:
        die("could not determine jj identity: set [jj] name/email in "
            f"{GLOBAL_CONFIG_PATH}, or configure it on this host with "
            "`jj config set --user user.name/user.email` (devbox falls back to that "
            "if [jj] isn't set)")
    return name, email


def resolve_env(project: ProjectConfig, gc: GlobalConfig) -> dict:
    """The effective `[env]` map for a box: the global creds/vars, with the
    project's `[env]` overriding per-key. Injected into the box's shell env (see
    sync.run_envvars) and exported into the provisioning run so mods that need a
    cred at provision time (e.g. repo-bootstrap's private `jj git clone`) see it."""
    return {**gc.env, **project.env}


# ---- mod resolution ---------------------------------------------------------
def effective_mods(project: ProjectConfig) -> list:
    """The ordered playbook for a project: the built-in base plus the project's
    own `mods`, appended (the 95% case: "just add rust")."""
    return [*DEFAULT_BASE_MODS, *project.mods]


def resolve_mod(name: str, marker_dir: Path,
                global_mods_dir: Optional[Path] = None,
                builtin_mods_dir: Optional[Path] = None) -> Path:
    """Locate a mod's script by name, precedence project > global > built-in, so
    a project can shadow a built-in (e.g. its own `dotfiles.sh`). The global /
    built-in dirs default to the module constants, resolved at call time (so
    tests can monkeypatch them)."""
    global_mods_dir = global_mods_dir or GLOBAL_MODS_DIR
    builtin_mods_dir = builtin_mods_dir or BUILTIN_MODS_DIR
    project_mods_dir = marker_dir / PROJECT_MARKER_DIR / PROJECT_MODS_SUBDIR
    for base in (project_mods_dir, global_mods_dir, builtin_mods_dir):
        candidate = base / f"{name}.sh"
        if candidate.is_file():
            return candidate
    die(f"mod '{name}' not found: looked in the project ({project_mods_dir}), "
        f"global ({global_mods_dir}), and built-ins ({builtin_mods_dir}). "
        f"Add {name}.sh to one of them, or fix the `mods` list in "
        f"{marker_dir / PROJECT_MARKER_DIR / 'config.toml'}.")


# ---- mod dependencies (`# requires:` header) --------------------------------
# A mod declares the other mods it depends on with a header comment line:
#   # requires: jj github-auth
# Names are space/comma-separated; multiple `# requires:` lines are unioned. The
# line holds ONLY dep names (the capture runs to end of line - no trailing inline
# comment). Parsed statically from the script text; a mod is never executed to
# read its metadata.
_REQUIRES_RE = re.compile(r"^#\s*requires:\s*(.*)$", re.MULTILINE)


def parse_requires(script_text: str) -> list:
    """The mods declared in a script's `# requires:` header line(s), in first-seen
    order (deduped). No such line -> []. Static parse only - never runs the mod."""
    seen = {}
    for match in _REQUIRES_RE.finditer(script_text):
        for name in match.group(1).replace(",", " ").split():
            seen.setdefault(name, None)
    return list(seen)


def validate_mods(project: ProjectConfig) -> None:
    """Fail fast if a mod is in the effective playbook without its declared
    dependencies (`# requires:`) ordered ahead of it. Deliberately NOT a resolver:
    no topo-sort, no auto-insert - just parse each mod's header and check order.
    Runs at playbook-resolution time (static, instant, before any boot/SSH).

    Validates the FULL configured `effective_mods(project)` - callers must pass the
    configured project even for selective runs (`devbox sync <subset>`), since deps
    are about the box's cumulative state, not the selected subset."""
    names = effective_mods(project)
    # First index wins on a duplicate; base always precedes project mods, so this
    # doesn't affect the ordering guarantee.
    index = {}
    for i, name in enumerate(names):
        index.setdefault(name, i)
    for i, name in enumerate(names):
        script = resolve_mod(name, project.marker_dir)
        for dep in parse_requires(script.read_text()):
            if dep not in index:
                die(f"mod '{name}' requires '{dep}', which isn't in the playbook: "
                    f"add '{dep}' to `mods` in "
                    f"{project.marker_dir / PROJECT_MARKER_DIR / 'config.toml'}.")
            if index[dep] > i:
                die(f"mod '{name}' requires '{dep}', but '{dep}' is listed after it "
                    f"in the playbook: move '{dep}' earlier in `mods`.")


# ---- environment context (not %TOKEN% substitution) -------------------------
def _provision_context(project: ProjectConfig, gc: GlobalConfig) -> dict:
    """The DEVBOX_* provisioning context the driver exposes to every mod
    (non-secret, safe to trace). User credentials/vars are delivered separately
    as the box's [env] file (see sync.run_envvars), not in the payload."""
    jj_name, jj_email = resolve_jj_identity(gc)
    return {
        "DEVBOX_USER": project.user,
        "DEVBOX_JJ_NAME": jj_name,
        "DEVBOX_JJ_EMAIL": jj_email,
        "DEVBOX_REPO_MODE": project.repo.mode,
        "DEVBOX_REPO_DIR": project.repo.dir,
        "DEVBOX_REPO_URL": project.repo.url or "",
    }


def render_env_file(env: dict) -> str:
    """A sourceable env file: `export KEY=<shell-quoted value>` per entry. Proper
    quoting (unlike %TOKEN% text substitution) means values with spaces/quotes/%
    survive intact."""
    return "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())


# ---- payload assembly -------------------------------------------------------
def build_payload(project: ProjectConfig, gc: GlobalConfig) -> list:
    """The full provisioning payload (driver + manifest + env + resolved mods),
    as PayloadFiles. Tarred and run over SSH by both `devbox create` and
    `devbox sync --mods` - the identical set either way."""
    if not DRIVER_PATH.is_file():
        die(f"devbox internal error: provisioning driver not found at "
            f"{DRIVER_PATH} (broken install?)")

    # Validate mod `# requires:` ordering over the FULL configured playbook. This
    # is the single choke point: build_payload always builds the whole manifest
    # (even for a selective `devbox sync <subset>`, which validates the full
    # playbook here and then runs only the selected subset), so deps are checked
    # against the box's cumulative state, not the subset.
    validate_mods(project)

    names = effective_mods(project)
    context = _provision_context(project, gc)

    payload = [
        PayloadFile("provision-driver.sh", DRIVER_PATH.read_text(), 0o755),
        PayloadFile("manifest", "".join(f"{n}\n" for n in names), 0o644),
        PayloadFile("context.env", render_env_file(context), 0o644),
    ]
    for name in names:
        script = resolve_mod(name, project.marker_dir)
        payload.append(PayloadFile(f"mods/{name}.sh", script.read_text(), 0o755))
    return payload


def build_tarball(payload: list) -> bytes:
    """A gzip tar of the payload, piped over SSH and extracted as the SSH user
    (so the 0600 secret file is user-owned, no chown needed). Deterministic
    (mtime=0) so identical inputs produce identical bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for pf in payload:
            data = pf.content.encode()
            info = tarfile.TarInfo(pf.path)
            info.size = len(data)
            info.mode = pf.mode
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
