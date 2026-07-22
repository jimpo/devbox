"""Modular provisioning: composable, idempotent "mods".

A **mod** is one idempotent bash script that installs/configures one thing (jj,
dotfiles, Claude Code, a project's rust toolchain, ...). A project's effective
playbook is the built-in base list plus the project's own `mods` (appended),
each resolved by name with precedence **project > global > built-in**:

    devbox/mods/*.sh              # built-in, shipped in the package
    ~/.config/devbox/mods/*.sh    # global user (shared across projects)
    .devbox/mods/*.sh             # project-local (can shadow a built-in by name)

Mods are *static files* (lintable, shellcheck-able, diffable) that receive
context via the **environment** (DEVBOX_USER, DEVBOX_JJ_NAME, ...) rather than
`%TOKEN%` substitution; the GitHub PAT goes in a mode-0600 env file the driver
sources without tracing, so it never lands in a script on disk or a log.

One driver (`provision-driver.sh`) runs the resolved set in order, two delivery
paths sharing this single source of truth:

  * first boot - the payload is written into the cloud-init seed's write_files
    and a runcmd invokes the driver (see cloudconfig.render_cloud_config).
  * `devbox provision` - the payload is tarred, piped over SSH, and the driver
    is run against the already-booted box - covering base AND project mods (the
    gap in the old monolithic-script model).
"""
from __future__ import annotations

import dataclasses
import io
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

from .config import GLOBAL_CONFIG_PATH, GlobalConfig, ProjectConfig, PROJECT_MARKER_DIR
from .util import die

# The built-in mods (shipped in the package) and the standalone driver.
BUILTIN_MODS_DIR = Path(__file__).parent / "mods"
DRIVER_PATH = Path(__file__).parent / "provision-driver.sh"
# Global user mods, shared across a user's projects.
GLOBAL_MODS_DIR = GLOBAL_CONFIG_PATH.parent / "mods"
# Project-local mods live under the marker dir: <marker>/.devbox/mods/*.sh.
PROJECT_MODS_SUBDIR = "mods"

# The default base playbook (built-ins, fixed order). Ordering constraints are
# encoded here: jj + github-auth must precede repo-bootstrap so a private
# `jj git clone` can authenticate. A project's `mods` are appended after this.
DEFAULT_BASE_MODS = ("jj", "github-auth", "dotfiles", "vim", "claude-code", "repo-bootstrap")

# Where the first-boot cloud-init seed stages the payload (root-owned; the
# driver runs as the dev user, who is chowned the 0600 secret file first). The
# reconverge path extracts into a per-run mktemp dir owned by the SSH user.
REMOTE_PROVISION_ROOT = "/usr/local/lib/devbox-provision"

# Provisioning output log, under the dev user's $HOME - written (tee'd) by both
# the first-boot runcmd and `devbox provision`, and tailed by `devbox status`.
# The driver additionally keeps a per-mod log at ~/provision/<name>.log.
PROVISION_LOG = "provision.log"


@dataclasses.dataclass
class PayloadFile:
    """One file in the provisioning payload, positioned relative to the payload
    root (the seed's REMOTE_PROVISION_ROOT, or the reconverge mktemp dir)."""
    path: str          # relative path, e.g. "provision-driver.sh" or "mods/jj.sh"
    content: str
    mode: int          # unix permission bits (e.g. 0o755, 0o600)
    secret: bool = False


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


def resolve_github_token(project: ProjectConfig, gc: GlobalConfig) -> Optional[str]:
    """[github] token, project overriding global: project's .devbox/config.toml
    -> global config -> none. Best-effort, not required: cloning a public repo
    or [repo] mode "push"/"none" never need GitHub HTTPS auth in the sandbox at
    all."""
    return project.github_token or gc.github_token


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


# ---- environment context (not %TOKEN% substitution) -------------------------
def _provision_env(project: ProjectConfig, gc: GlobalConfig) -> tuple:
    """(context, secret) env dicts the driver exposes to every mod. Context is
    non-secret (safe to trace); secret holds only the GitHub PAT, kept out of
    any log. Same resolution for the first-boot render and `devbox provision`."""
    jj_name, jj_email = resolve_jj_identity(gc)
    github_token = resolve_github_token(project, gc)
    if github_token is None:
        print("note: no GitHub token found ([github].token in the project's "
              ".devbox/config.toml or the global config) - the sandbox will have "
              "no GitHub HTTPS auth. Fine for cloning public repos; set one up if "
              "the project needs a private repo or gh/git pushes to GitHub.")
    context = {
        "DEVBOX_USER": project.user,
        "DEVBOX_JJ_NAME": jj_name,
        "DEVBOX_JJ_EMAIL": jj_email,
        "DEVBOX_REPO_MODE": project.repo.mode,
        "DEVBOX_REPO_DIR": project.repo.dir,
        "DEVBOX_REPO_URL": project.repo.url or "",
    }
    secret = {"DEVBOX_GITHUB_TOKEN": github_token} if github_token else {}
    return context, secret


def render_env_file(env: dict) -> str:
    """A sourceable env file: `export KEY=<shell-quoted value>` per entry. Proper
    quoting (unlike %TOKEN% text substitution) means values with spaces/quotes/%
    survive intact."""
    return "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())


# ---- payload assembly -------------------------------------------------------
def build_payload(project: ProjectConfig, gc: GlobalConfig) -> list:
    """The full provisioning payload (driver + manifest + env + resolved mods),
    as PayloadFiles positioned under the payload root. Shared by both delivery
    paths so first-boot and reconverge run the identical set."""
    if not DRIVER_PATH.is_file():
        die(f"devbox internal error: provisioning driver not found at "
            f"{DRIVER_PATH} (broken install?)")

    names = effective_mods(project)
    context, secret = _provision_env(project, gc)

    payload = [
        PayloadFile("provision-driver.sh", DRIVER_PATH.read_text(), 0o755),
        PayloadFile("manifest", "".join(f"{n}\n" for n in names), 0o644),
        PayloadFile("context.env", render_env_file(context), 0o644),
    ]
    if secret:
        payload.append(PayloadFile("secret.env", render_env_file(secret), 0o600, secret=True))
    for name in names:
        script = resolve_mod(name, project.marker_dir)
        payload.append(PayloadFile(f"mods/{name}.sh", script.read_text(), 0o755))
    return payload


def build_tarball(payload: list) -> bytes:
    """A gzip tar of the payload, for `devbox provision` to pipe over SSH and
    extract (as the SSH user, so the 0600 secret file is user-owned with no
    chown needed). Deterministic (mtime=0) so identical inputs produce identical
    bytes."""
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
