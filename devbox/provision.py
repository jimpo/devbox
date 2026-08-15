"""Modular provisioning: composable, idempotent "mods".

A **mod** is one idempotent bash script that installs/configures one thing (jj,
dotfiles, Claude Code, a project's rust toolchain, ...). A project's effective
playbook is the built-in base list plus the project's own `mods` (appended),
each resolved by name with precedence **project > global > built-in**:

    mods/*.sh                     # built-in, at the repo root
    ~/.config/devbox/mods/*.sh    # global user (shared across projects)
    .devbox/mods/*.sh             # project-local (can shadow a built-in by name)

Mods are *static files* (lintable, shellcheck-able, diffable, directly runnable)
that receive context via the **environment** (DEVBOX_AGENT_USER, DEVBOX_JJ_NAME,
...) rather than `%TOKEN%` substitution, and declare their metadata in header
comments (`# requires:`, `# runas:`, `# packages:`). The GitHub PAT goes in a
mode-0600 env file the driver sources without tracing, so it never lands in a
script on disk or a log.

**Two accounts.** A box has a privileged user (`user`, the SSH target
provisioning runs through) and an unprivileged one (`agent_user`, no sudo) that
AI agents and the repo checkout live under. The driver runs as the privileged
user and drops to the agent for each mod body, because that is what a mod almost
always configures: a user's home directory. A mod body therefore CANNOT sudo -
system packages are declared with `# packages:` and installed by the driver, and
the rare mod that genuinely needs privilege says `# runas: root`.

This buys containment of *system integrity*, not of *credential exfiltration*:
the agent still has network egress and still holds every `[env]` cred it owns.
What it cannot do is tamper with the provisioner, the system, or the privileged
user's home - nor read a cred an `[env]` entry gives to the admin instead
(`owner = "admin"`, for the creds a privileged mod needs and agents do not).

One driver (`provision-driver.sh`) runs the resolved set in order. Provisioning
is delivered entirely over SSH (single path): devbox tars the payload
(`build_tarball`), pipes it to the box, extracts into a per-run temp dir, and
runs the driver - on `devbox create` (after boot) and on `devbox sync --mods`
alike (see cli.py). The payload carries NO secrets, which is what lets it be
readable by the agent account the driver drops into. The init script only does
base setup for the privileged user (user/hostname/BASH_ENV/packages); it carries
no provisioning payload, and does not create the agent user - the driver does,
so that changing `agent_user` reconverges instead of needing a rebuild.
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
# `jj git clone` can authenticate, and dotfiles-packages (the root half, apt)
# must precede dotfiles (the per-user half). A project's `mods` are appended
# after this.
DEFAULT_BASE_MODS = ("jj", "github-auth", "dotfiles-packages", "dotfiles", "vim",
                     "neovim", "claude-code", "repo-bootstrap")

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
    """The effective `{KEY: EnvVar}` map for a box: the global creds/vars, with
    the project's `[env]` overriding per-key (whole entry, owner included). Split
    by owner into one shell-env file per account (see sync.run_envvars), which is
    also what a mod sources at provision time - so a mod that needs a cred then
    (e.g. repo-bootstrap's private `jj git clone`) sees the ones its own account
    owns."""
    return {**gc.env, **project.env}


def env_for_owner(env: dict, owner: str) -> dict:
    """The `{KEY: value}` slice of an effective `[env]` map belonging to one
    account - what actually goes into that account's env file."""
    return {key: var.value for key, var in env.items() if var.owner == owner}


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


# ---- mod metadata (`# requires:` / `# runas:` / `# packages:` headers) -------
# A mod declares its metadata in header comment lines, e.g.
#   # requires: jj github-auth
#   # runas: agent
#   # packages: jq unzip
# Values are space/comma-separated; multiple lines of the same key are unioned.
# A line holds ONLY values (the capture runs to end of line - no trailing inline
# comment). Parsed statically from the script text; a mod is never executed to
# read its metadata. Comment headers rather than a structured front matter keep
# a mod a plain script: directly runnable, shellcheck-able, shebang on line 1.
_HEADER_RE_CACHE: dict = {}


def _header_re(key: str):
    if key not in _HEADER_RE_CACHE:
        _HEADER_RE_CACHE[key] = re.compile(rf"^#\s*{key}:\s*(.*)$", re.MULTILINE)
    return _HEADER_RE_CACHE[key]


def parse_header(script_text: str, key: str) -> list:
    """The values of a mod's `# <key>:` header line(s), in first-seen order
    (deduped). No such line -> []. Static parse only - never runs the mod."""
    seen = {}
    for match in _header_re(key).finditer(script_text):
        for value in match.group(1).replace(",", " ").split():
            seen.setdefault(value, None)
    return list(seen)


def parse_requires(script_text: str) -> list:
    """The mods a script declares as dependencies (`# requires:`)."""
    return parse_header(script_text, "requires")


# Who a mod's body runs as. `agent` (the default) is the unprivileged account
# that AI agents and the repo checkout live under - the right target for the
# overwhelming majority of mods, which configure a user's home directory.
# `admin` is the privileged account, `root` is for the rare mod that genuinely
# needs privilege for something `# packages:` can't express. A mod may name more
# than one, and then runs once per target (e.g. dotfiles, for both humans' sake).
RUNAS_AGENT = "agent"
RUNAS_ADMIN = "admin"
RUNAS_ROOT = "root"
RUNAS_TARGETS = (RUNAS_AGENT, RUNAS_ADMIN, RUNAS_ROOT)
DEFAULT_RUNAS = (RUNAS_AGENT,)


def parse_runas(script_text: str) -> list:
    """The accounts a mod's body runs as (`# runas:`), defaulting to the agent."""
    return parse_header(script_text, "runas") or list(DEFAULT_RUNAS)


def parse_packages(script_text: str) -> list:
    """The apt packages a mod needs (`# packages:`), installed by the driver as
    the privileged user before the body runs. This header exists because a mod
    body runs without sudo: it cannot install its own dependencies."""
    return parse_header(script_text, "packages")


@dataclasses.dataclass
class ModSpec:
    """One resolved mod: where its script is and what its headers declared."""
    name: str
    path: Path
    requires: list
    runas: list
    packages: list


def load_mod_specs(project: ProjectConfig) -> list:
    """Resolve every mod in the project's effective playbook and parse its
    headers, in playbook order. One read per mod, shared by validation and
    payload assembly."""
    specs = []
    for name in effective_mods(project):
        path = resolve_mod(name, project.marker_dir)
        text = path.read_text()
        runas = parse_runas(text)
        unknown = [r for r in runas if r not in RUNAS_TARGETS]
        if unknown:
            die(f"mod '{name}' has an unknown `# runas:` target "
                f"{', '.join(repr(u) for u in unknown)} ({path}); "
                f"valid targets are {', '.join(RUNAS_TARGETS)}.")
        specs.append(ModSpec(name=name, path=path, requires=parse_requires(text),
                             runas=runas, packages=parse_packages(text)))
    return specs


def _validate_ordering(specs: list, project: ProjectConfig) -> None:
    """Fail fast if a mod is in the effective playbook without its declared
    dependencies (`# requires:`) ordered ahead of it. Deliberately NOT a resolver:
    no topo-sort, no auto-insert - just check the parsed headers against order."""
    # First index wins on a duplicate; base always precedes project mods, so this
    # doesn't affect the ordering guarantee.
    index = {}
    for i, spec in enumerate(specs):
        index.setdefault(spec.name, i)
    for i, spec in enumerate(specs):
        for dep in spec.requires:
            if dep not in index:
                die(f"mod '{spec.name}' requires '{dep}', which isn't in the playbook: "
                    f"add '{dep}' to `mods` in "
                    f"{project.marker_dir / PROJECT_MARKER_DIR / 'config.toml'}.")
            if index[dep] > i:
                die(f"mod '{spec.name}' requires '{dep}', but '{dep}' is listed after it "
                    f"in the playbook: move '{dep}' earlier in `mods`.")


def validate_mods(project: ProjectConfig) -> None:
    """Validate the playbook's metadata: `# runas:` targets (in load_mod_specs)
    and `# requires:` ordering. Runs at playbook-resolution time (static,
    instant, before any boot/SSH).

    Validates the FULL configured `effective_mods(project)` - callers must pass the
    configured project even for selective runs (`devbox sync <subset>`), since deps
    are about the box's cumulative state, not the selected subset."""
    _validate_ordering(load_mod_specs(project), project)


# ---- environment context (not %TOKEN% substitution) -------------------------
def _provision_context(project: ProjectConfig, gc: GlobalConfig) -> dict:
    """The DEVBOX_* provisioning context the driver exposes to every mod
    (non-secret, safe to trace). User credentials/vars are delivered separately
    as the box's [env] file (see sync.run_envvars), not in the payload."""
    jj_name, jj_email = resolve_jj_identity(gc)
    return {
        # Both accounts, named explicitly: a bare DEVBOX_USER would be ambiguous
        # now that a box has a privileged user and an unprivileged agent.
        "DEVBOX_ADMIN_USER": project.user,
        "DEVBOX_AGENT_USER": project.agent_user,
        # The host's public key, so the driver can authorize the same key for the
        # agent account it creates. Public by definition - safe in the payload,
        # which carries no secrets.
        "DEVBOX_SSH_PUBKEY": gc.read_ssh_pubkey(),
        "DEVBOX_JJ_NAME": jj_name,
        "DEVBOX_JJ_EMAIL": jj_email,
        "DEVBOX_REPO_MODE": project.repo.mode,
        "DEVBOX_REPO_DIR": project.repo.dir,
        "DEVBOX_REPO_URL": project.repo.url or "",
    }


def render_manifest(specs: list) -> str:
    """The payload's `manifest`: one tab-separated record per mod, in playbook
    order - `<name>\\t<runas,...>\\t<packages,...>`. Tab-separated with
    comma-joined lists so the driver can read it with plain `IFS` splitting; it
    has no YAML/JSON parser and should not grow one."""
    return "".join(
        f"{spec.name}\t{','.join(spec.runas)}\t{','.join(spec.packages)}\n"
        for spec in specs
    )


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

    # Validate the FULL configured playbook's metadata (`# runas:` targets and
    # `# requires:` ordering). This is the single choke point: build_payload
    # always builds the whole manifest (even for a selective `devbox sync
    # <subset>`, which validates the full playbook here and then runs only the
    # selected subset), so deps are checked against the box's cumulative state,
    # not the subset.
    specs = load_mod_specs(project)
    _validate_ordering(specs, project)

    context = _provision_context(project, gc)

    payload = [
        PayloadFile("provision-driver.sh", DRIVER_PATH.read_text(), 0o755),
        PayloadFile("manifest", render_manifest(specs), 0o644),
        PayloadFile("context.env", render_env_file(context), 0o644),
    ]
    for spec in specs:
        payload.append(PayloadFile(f"mods/{spec.name}.sh", spec.path.read_text(), 0o755))
    return payload


def build_tarball(payload: list) -> bytes:
    """A gzip tar of the payload, piped over SSH and extracted as the SSH user
    into a per-run temp dir the driver then makes agent-readable (the payload
    carries no secrets - see the module docstring). Deterministic (mtime=0) so
    identical inputs produce identical bytes."""
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
