"""Modular provisioning: composable, idempotent "mods".

A **mod** is one idempotent bash script that installs/configures one thing (jj,
dotfiles, Claude Code, a workspace's rust toolchain, ...). A workspace's
effective playbook is the built-in base list plus the workspace's own `mods`
(appended), each resolved by name with precedence
**workspace > global > built-in**:

    devbox/mods/                  # built-in, inside the package
    ~/.config/devbox/mods/        # global user (shared across workspaces)
    <workspace mods dir>/         # workspace-local (can shadow a built-in)

Each of those holds BOTH halves of a mod - `<name>.sh` and `<name>.nix` - so
one tuple of directories serves both lookups (see `mods_dirs`). The
workspace's own is `.devbox/mods/` or `mods/`, whichever the marker spelling
implies (see config.Marker) - resolved once, at config time.

Mods are *static files* (lintable, shellcheck-able, diffable, directly runnable)
that receive context via the **environment** (DEVBOX_AGENT_USER, DEVBOX_JJ_NAME,
...) rather than `%TOKEN%` substitution, and declare their metadata (`requires`,
`runas`, `packages`) in a fenced `# /// mod` block of TOML - PEP 723's shape, so
a mod stays runnable and shellcheck-able. The GitHub PAT goes in a
mode-0600 env file the driver sources without tracing, so it never lands in a
script on disk or a log.

**Accounts.** A box has one privileged user (the SSH target provisioning runs
through) and one unprivileged `agent_user` per attached workspace, with no sudo,
which that workspace's AI agents and checkouts live under. Every repo in one
workspace shares that account, and so shares its git/jj credentials. The driver runs as
the privileged user and drops to the agent for each mod body, because that is
what a mod almost always configures: a user's home directory. A mod body
therefore CANNOT sudo - system packages are declared with `packages` and
installed by the driver, and the rare mod that genuinely needs privilege says
`runas = ["root"]`.

**Two scopes.** A box playbook runs once per box (at `devbox box create` /
`devbox box sync`) and a workspace playbook runs once per attachment (at `devbox
create` / `devbox sync --mods`). `runas` already says which half of a mod
belongs where, so scoping is a host-side filter over the targets it declares
(see `scoped_runas`) rather than anything the driver has to understand: the
manifest it receives has already been narrowed to the current scope.

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

The workspace's checkouts ride along as a `repos` payload file rather than as
context variables, since there can be any number of them; `repo-bootstrap` reads
it through DEVBOX_PAYLOAD_DIR, which the driver exports into each mod.

**The declarative half.** An attachment's payload also carries a home-manager
flake (`nix/`, from NIX_DIR), which `mods/home-manager.sh` switches the agent
account to. A playbook name resolves to ONE of the two halves (see
`resolve_playbook`): a `<name>.sh` in any mods directory, or - only when no
directory has one - a `<name>.nix`, same **workspace > global > built-in**
precedence as the bash side, out of the SAME three directories (see
`mods_dirs`): every level holds both halves, devbox's own built-ins included,
so `<name>.sh` and `<name>.nix` sit next to each other and "a script wins" is
a rule about one directory listing. So converting a mod, or writing a
workspace's own module from scratch, is a one-file change either way, and a
workspace that ships its own `<name>.sh` still gets its own, converted or not.
A mod is a bash script or a module, never both.

NIX_DIR holds only the flake SKELETON (`flake.nix`, `options.nix`,
`zshenv.nix`); `nix/modules/` exists only in an assembled payload, where
`nix_payload_files` writes one file per resolved module whatever directory it
came from. That assembled layout is the only thing nix ever evaluates, which
is what frees the three source directories to hold both halves - and it means
NIX_DIR alone cannot be evaluated at all (options.nix imports the
`workspace.nix` only a payload has). `devbox nix check` assembles one and runs
nix against it; there is no other way to evaluate a module off a box.

`nix/workspace.nix` is rendered per attachment (see render_workspace_nix) the
way `manifest` and `repos` are, and carries no creds - /nix/store is
world-readable and shared by every attachment on the box. The flake is
agent-only: a box run ships none of it, and the privileged account stays on
apt.
"""
from __future__ import annotations

import dataclasses
import io
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path
from typing import Optional

from .config import GLOBAL_CONFIG_PATH, BoxSpec, GlobalConfig, WorkspaceConfig
from .util import die

# The built-in mods and the standalone driver, inside the package so that
# `packages = ["devbox"]` ships them (see pyproject.toml). BOTH halves of a mod
# live here, `<name>.sh` and `<name>.nix`, the way they do in a workspace's own
# mods directory and in the user's global one - one directory per precedence
# level, so `<name>.sh` and `<name>.nix` sit next to each other and "a script
# wins" is a rule about one listing.
BUILTIN_MODS_DIR = Path(__file__).parent / "mods"
DRIVER_PATH = Path(__file__).parent / "provision-driver.sh"
# The home-manager flake SKELETON: `flake.nix`, `options.nix` and `zshenv.nix`,
# and nothing else. It is a template rather than a working flake - it cannot be
# evaluated as it sits, because options.nix imports the `workspace.nix` only a
# rendered payload has. `modules/` exists only in an assembled payload, where
# nix_payload_files writes one file per resolved module name. `devbox nix
# check` (cli.cmd_nix_check) is what assembles the two into a flake and
# evaluates it.
NIX_DIR = Path(__file__).parent / "nix"
# Where the flake lands in the payload, next to `mods/`. The home-manager mod
# names it through DEVBOX_PAYLOAD_DIR.
NIX_PAYLOAD_DIR = "nix"
# The one file in that directory devbox renders rather than ships (see
# render_workspace_nix), named here so a stray copy in NIX_DIR cannot shadow it.
NIX_WORKSPACE_FILE = "workspace.nix"
# Global user mods, shared across a user's workspaces - both halves, the way a
# workspace's own mods directory holds both: `<name>.sh` and `<name>.nix`.
GLOBAL_MODS_DIR = GLOBAL_CONFIG_PATH.parent / "mods"

# The box playbook: what a machine needs once, regardless of who lives on it -
# the apt half of the dotfiles setup (`dotfiles-packages`, required by the
# agent's own `dotfiles` mod) and the nix daemon. The privileged account gets
# no configuration beyond that and no generation of its own: it is the box's
# debugging entry point (`devbox ssh --admin`) and deliberately stays
# independent of anything that could itself need fixing - stock apt bash,
# unconfigured apt vim, no oh-my-zsh, no vim-plug, no git credential helper.
# `nvim` and every agent's configured vim/git come from that attachment's
# home-manager generation instead, so they live in that agent's profile and
# nowhere else.
# `nix` is box-scoped because /nix and the nix-daemon are one machine-wide
# store that every attached workspace shares, and only the privileged account
# is a trusted user of it.
# Fixed and built-in: there is no box config file, so a box grows beyond this
# only when an attached workspace contributes a `runas = ["root"]` mod.
BOX_BASE_MODS = ("dotfiles-packages", "nix")

# The workspace playbook: what one attachment needs inside its own account.
# Ordering constraints are encoded here: jj must precede repo-bootstrap (which
# clones with it). `dotfiles` is agent-only now - the privileged account gets
# none of it (see BOX_BASE_MODS). A workspace's `mods` are appended after this.
# `home-manager` runs first: it is the declarative half of the account, and a
# bash mod after it can use what the generation put on the PATH. `vim` and
# `github-auth` are names that half provides: no mods directory has a vim.sh or
# a github-auth.sh, so they resolve to the built-in vim.nix and github-auth.nix
# sitting beside the scripts (see resolve_playbook). `github-auth` lives at
# ~/.config/git/config rather than ~/.gitconfig - which `dotfiles`/configenv
# overwrites - so it needs no root and no box-wide /etc/gitconfig any more.
WORKSPACE_BASE_MODS = ("home-manager", "github-auth", "jj", "dotfiles", "vim",
                       "neovim", "claude-code", "cargo-binstall",
                       "repo-bootstrap")

# Which playbook a provisioning run is executing. The scope decides which
# `runas` targets of each mod run (see scoped_runas); it is resolved entirely
# host-side, so the driver just runs the manifest it is handed.
SCOPE_BOX = "box"
SCOPE_WORKSPACE = "workspace"

def provision_log(agent_user: str = "") -> str:
    """The combined provisioning log for one run, under the privileged user's
    $HOME - written (tee'd) when the driver runs over SSH, and tailed by
    `devbox status`. Per attachment (the driver keeps its per-mod logs in a
    matching `provision/<agent|box>/` directory), so two workspaces reconverging
    on one box don't overwrite each other's."""
    return f"provision-{agent_user}.log" if agent_user else "provision.log"


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


def resolve_env(workspace: WorkspaceConfig, gc: GlobalConfig) -> dict:
    """The effective `{KEY: EnvVar}` map for a box: the global creds/vars, with
    the workspace's `[env]` overriding per-key (whole entry, owner included). Split
    by owner into one shell-env file per account (see sync.run_envvars), which is
    also what a mod sources at provision time - so a mod that needs a cred then
    (e.g. repo-bootstrap's private `jj git clone`) sees the ones its own account
    owns."""
    return {**gc.env, **workspace.env}


def env_for_owner(env: dict, owner: str) -> dict:
    """The `{KEY: value}` slice of an effective `[env]` map belonging to one
    account - what actually goes into that account's env file."""
    return {key: var.value for key, var in env.items() if var.owner == owner}


# ---- mod resolution ---------------------------------------------------------
# A playbook name resolves to ONE of two things: a bash script the driver runs,
# or a nix module the agent's home-manager generation provides. Which, is
# decided by file presence alone - `<name>.sh` in any mods directory, else
# `<name>.nix` in the flake's - so converting a mod is a one-file change with no
# registry to keep in step.
#
# SCRIPTS WIN, at every level of the existing precedence (workspace > global >
# built-in). A workspace that ships its own `jj.sh` gets its own jj, converted
# or not, and the module of that name stays inert - the alternative, running
# both, is the one outcome that is clearly wrong. This is also what makes a
# conversion safe to land: until the built-in `<name>.sh` is deleted, the bash
# mod is still what runs.
#
# The BOX playbook never consults the modules directory. The flake describes one
# agent's home, a box run has no agent, and a box mod is system work no
# generation could do.
def box_playbook() -> list:
    """The ordered playbook for a box itself. Fixed: a box takes no config."""
    return list(BOX_BASE_MODS)


def workspace_playbook(workspace: WorkspaceConfig) -> list:
    """The ordered playbook for one attachment: the built-in workspace base plus
    the workspace's own `mods`, appended (the 95% case: "just add rust")."""
    return [*WORKSPACE_BASE_MODS, *workspace.mods]


def mods_dirs(mods_dir: Optional[Path], global_mods_dir: Optional[Path] = None,
              builtin_mods_dir: Optional[Path] = None) -> tuple:
    """The three directories a mod can come from, in precedence order:
    workspace > global > built-in. Each holds BOTH halves, so this one tuple
    serves find_mod_script and find_nix_module alike - they differ only in the
    suffix they look for. The global/built-in defaults resolve at call time, so
    tests can monkeypatch the constants; `mods_dir` is None for a box-scope
    run, which has no workspace to shadow from."""
    return (mods_dir,
            global_mods_dir or GLOBAL_MODS_DIR,
            builtin_mods_dir or BUILTIN_MODS_DIR)


def nix_module_names(mods_dir: Optional[Path] = None,
                     global_mods_dir: Optional[Path] = None,
                     builtin_mods_dir: Optional[Path] = None) -> list:
    """Every mod name any mods directory provides a `<name>.nix` for, sorted
    and de-duplicated. Which FILE actually answers to a given name (workspace >
    global > built-in) is find_nix_module's job, not this one - a name can
    appear here because only the built-in ships it, even though a workspace's
    own file of the same name is what will actually be used. `mods_dir` is None
    for a box-scope run, which has no workspace and no flake at all.

    EVERY `.nix` file in a mods directory is a mod's module. There is no other
    kind: one lands in the payload and the flake imports it, so a `.nix` that
    is not a home-manager module fails the whole activation. A helper a mod
    needs belongs somewhere else."""
    names = set()
    for base in mods_dirs(mods_dir, global_mods_dir, builtin_mods_dir):
        if base is not None and base.is_dir():
            names.update(p.stem for p in base.glob("*.nix"))
    return sorted(names)


def find_nix_module(name: str, mods_dir: Optional[Path],
                    global_mods_dir: Optional[Path] = None,
                    builtin_mods_dir: Optional[Path] = None) -> Optional[Path]:
    """Where a mod's nix module is, by precedence workspace > global >
    built-in - the same precedence out of the same directories find_mod_script
    uses for bash mods, so a workspace (or a user's global config) can ship its
    own module, or shadow a built-in one, exactly the way it can ship its own
    `<name>.sh`. `None` if no directory has one."""
    for base in mods_dirs(mods_dir, global_mods_dir, builtin_mods_dir):
        if base is None:
            continue
        candidate = base / f"{name}.nix"
        if candidate.is_file():
            return candidate
    return None


def find_mod_script(name: str, mods_dir: Optional[Path],
                    global_mods_dir: Optional[Path] = None,
                    builtin_mods_dir: Optional[Path] = None) -> Optional[Path]:
    """Where a mod's bash script is, by precedence workspace > global >
    built-in - so a workspace can shadow a built-in (e.g. its own
    `dotfiles.sh`) - or None if no directory has one. A script found here wins
    over a module of the same name sitting right beside it (see
    resolve_playbook)."""
    for base in mods_dirs(mods_dir, global_mods_dir, builtin_mods_dir):
        if base is None:
            continue
        candidate = base / f"{name}.sh"
        if candidate.is_file():
            return candidate
    return None


# Mods that a conversion folded into another one, and so answer to no file
# anywhere. A workspace naming a retired mod is told where it went instead of
# being sent to look for a script that is never coming back. It is still a hard
# error, and it is the last thing consulted: a `<name>.sh` in any mods directory
# answers to the name first and wins, exactly as it does for any other name.
RETIRED_MODS = {
    "cloudflared-tunnel":
        "`cloudflared`, which now installs the binary and runs the tunnel as a "
        "systemd user service",
}


def resolve_mod(name: str, mods_dir: Optional[Path],
                global_mods_dir: Optional[Path] = None,
                builtin_mods_dir: Optional[Path] = None,
                nix_modules: bool = False) -> Path:
    """Locate a mod's script by name (see find_mod_script), or die naming every
    place a `<name>.sh` (or, with `nix_modules=True`, a `<name>.nix`) could
    have been.

    `nix_modules=True` widens that message to say a nix module answers to a
    name too - passed for a workspace-scope run, where one can, and left False
    for a box, where the flake plays no part. A name a module DOES answer to
    never reaches here: resolve_playbook takes it out of the script half
    first."""
    path = find_mod_script(name, mods_dir, global_mods_dir, builtin_mods_dir)
    if path is not None:
        return path
    if name in RETIRED_MODS:
        die(f"mod '{name}' no longer exists on its own: it is part of "
            f"{RETIRED_MODS[name]}. Drop '{name}' from the `mods` list in the "
            f"config.")
    # Both halves of a mod live in these same directories, so the message names
    # three places whichever half the author meant, not six.
    dirs = ", ".join(str(d) for d in
                     mods_dirs(mods_dir, global_mods_dir, builtin_mods_dir)
                     if d is not None)
    wanted = f"{name}.sh or {name}.nix" if nix_modules else f"{name}.sh"
    die(f"mod '{name}' not found: looked for {wanted} in {dirs}. "
        f"Add it to one of them, or fix the `mods` list in the config.")


# ---- mod metadata (the `# /// mod` block) ------------------------------------
# A mod declares its metadata in ONE fenced block of comments holding TOML:
#
#   # /// mod
#   # requires = ["jj", "github-auth"]
#   # runas = ["admin", "agent"]
#   # packages = ["jq", "unzip"]
#   # ///
#
# The shape is PEP 723's (Python's inline script metadata, what `uv run` reads),
# which is also LSB init scripts' `### BEGIN INIT INFO` and mise's `#MISE` - the
# settled answer for "executable script that also carries metadata". Two
# properties earn it:
#
#   Fenced, so the boundary is explicit. The previous format matched
#   `^# <key>:` anywhere in the file, which made any prose comment that happened
#   to start that way silently become metadata - several mods discuss
#   `# packages:` in their header prose and were saved only by not starting a
#   line with it.
#
#   Comment-prefixed, so a mod stays a plain script: shebang on line 1,
#   `shellcheck mods/*.sh` clean, `./mods/jj.sh` still runs. The payload ships
#   the file verbatim and the driver just executes it.
#
# TOML rather than YAML because `tomllib` is stdlib on our floor (3.11), so it
# costs no dependency, and because it is already the language of devbox's own
# config files. Parsed statically; a mod is never executed to read its metadata.
MOD_METADATA_TYPE = "mod"
MOD_METADATA_KEYS = ("requires", "runas", "packages")

# PEP 723's reference regex, with the type left general so a mislabeled fence
# (`# /// script`) is found and reported rather than silently ignored.
_MOD_BLOCK_RE = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")

# The header format this replaced, recognized only to reject it (see below).
_LEGACY_HEADER_RE = re.compile(
    rf"^#\s*(?P<key>{'|'.join(MOD_METADATA_KEYS)}):.*$", re.MULTILINE)


def _unprefix(content: str) -> str:
    """Strip the comment prefix from a fenced block's lines, per PEP 723: drop
    two characters when the second is a space, otherwise one (a bare `#`)."""
    return "".join(line[2:] if line[1:2] == " " else line[1:]
                   for line in content.splitlines(keepends=True))


def parse_mod_metadata(script_text: str, path=None) -> dict:
    """The `{key: [value, ...]}` a mod's `# /// mod` block declares, validated.
    No block -> {} (every key defaults). Static parse only - never runs the mod.

    Validating here rather than at use sites means a typo (`require`, a bare
    string instead of an array) fails at playbook-resolution time with the file
    named, before any boot or SSH."""
    where = f" ({path})" if path else ""
    blocks = list(_MOD_BLOCK_RE.finditer(script_text))
    mine = [m for m in blocks if m.group("type") == MOD_METADATA_TYPE]
    if not mine:
        # A fence of the wrong type is a typo, not "no metadata": failing here
        # beats silently running the mod with every default.
        if blocks:
            die(f"mod metadata{where}: found a `# /// {blocks[0].group('type')}` "
                f"block, but devbox reads `# /// {MOD_METADATA_TYPE}`.")
        # Same reasoning for the format this replaced: an unfenced `# runas: root`
        # would now parse as no metadata at all and quietly run the mod as the
        # agent, which is the one failure mode a silent default must not have.
        legacy = _LEGACY_HEADER_RE.search(script_text)
        if legacy:
            die(f"mod metadata{where}: `{legacy.group(0).strip()}` is the old "
                f"header format, which devbox no longer reads. Move it into a "
                f"block:\n\n    # /// {MOD_METADATA_TYPE}\n"
                f'    # {legacy.group("key")} = ["..."]\n    # ///')
        return {}
    if len(mine) > 1:
        die(f"mod metadata{where}: more than one `# /// {MOD_METADATA_TYPE}` "
            f"block; a mod declares its metadata once.")

    try:
        data = tomllib.loads(_unprefix(mine[0].group("content")))
    except tomllib.TOMLDecodeError as exc:
        # The line/column in `exc` is relative to the block, not the file, so
        # name the block rather than letting it read as a file position.
        die(f"the `# /// {MOD_METADATA_TYPE}` block{where} is not valid TOML: {exc}")

    unknown = [k for k in data if k not in MOD_METADATA_KEYS]
    if unknown:
        die(f"mod metadata{where} has unknown key(s) "
            f"{', '.join(repr(k) for k in unknown)}; "
            f"valid keys are {', '.join(MOD_METADATA_KEYS)}.")
    for key, value in data.items():
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            die(f"mod metadata{where}: `{key}` must be an array of strings, "
                f'e.g. `{key} = ["a", "b"]`.')
    return data


# Who a mod's body runs as. `agent` (the default) is the unprivileged account
# that AI agents and the repo checkout live under - the right target for the
# overwhelming majority of mods, which configure a user's home directory.
# `admin` is the privileged account, `root` is for the rare mod that genuinely
# needs privilege for something `packages` can't express. A mod may name more
# than one, and then runs once per target (e.g. dotfiles, for both humans' sake).
RUNAS_AGENT = "agent"
RUNAS_ADMIN = "admin"
RUNAS_ROOT = "root"
RUNAS_TARGETS = (RUNAS_AGENT, RUNAS_ADMIN, RUNAS_ROOT)
DEFAULT_RUNAS = (RUNAS_AGENT,)


@dataclasses.dataclass
class ModSpec:
    """One resolved mod: where its script is and what its metadata declared."""
    name: str
    path: Path
    requires: list
    runas: list
    packages: list


def load_mod_specs(names: list, mods_dir: Optional[Path],
                   nix_modules: bool = False) -> list:
    """Resolve every named mod to its bash script and parse its metadata, in
    playbook order. One read per mod, shared by validation and payload assembly.

    Every name here is a script: resolve_playbook has already taken out the ones
    the generation provides. `nix_modules` only reaches the failure message,
    which then says a module could have answered too."""
    specs = []
    for name in names:
        path = resolve_mod(name, mods_dir, nix_modules=nix_modules)
        meta = parse_mod_metadata(path.read_text(), path)
        runas = meta.get("runas") or list(DEFAULT_RUNAS)
        unknown = [r for r in runas if r not in RUNAS_TARGETS]
        if unknown:
            die(f"mod '{name}' has an unknown `runas` target "
                f"{', '.join(repr(u) for u in unknown)} ({path}); "
                f"valid targets are {', '.join(RUNAS_TARGETS)}.")
        specs.append(ModSpec(name=name, path=path,
                             requires=meta.get("requires", []), runas=runas,
                             packages=meta.get("packages", [])))
    return specs


@dataclasses.dataclass
class Playbook:
    """One playbook, resolved into its two halves: the mods that are bash
    scripts the driver runs (`specs`, in playbook order) and the names the
    agent's home-manager generation provides instead (`modules`).

    A module name produces no ModSpec and no manifest line, so the driver never
    hears about it - which is the whole point: a converted mod stops being a
    step in a run and becomes part of one generation."""
    specs: list
    modules: list


def resolve_playbook(names: list, mods_dir: Optional[Path],
                     scope: str = SCOPE_WORKSPACE) -> Playbook:
    """Split a playbook by where each name comes from: a bash script if any mods
    directory has one, else a nix module of that name (workspace > global >
    built-in - see find_nix_module), else a hard error naming every place that
    was looked in.

    Called with the FULL configured playbook, so a `requires` on a converted mod
    still resolves (see validate_playbooks). `mods_dir` is the one directory a
    workspace contributes to either half, and at box scope it is None and no
    module is consulted at all."""
    is_workspace = scope == SCOPE_WORKSPACE
    provided = set(nix_module_names(mods_dir)) if is_workspace else set()
    scripts, modules = [], []
    for name in names:
        if name in provided and find_mod_script(name, mods_dir) is None:
            modules.append(name)
        else:
            scripts.append(name)
    return Playbook(specs=load_mod_specs(scripts, mods_dir, nix_modules=is_workspace),
                    modules=modules)


def scoped_runas(runas: list, scope: str) -> list:
    """The `runas` targets of a mod that actually run in `scope`.

    A mod that names the agent has an agent half, and that half belongs to a
    workspace: at box scope it runs everything *except* the agent (so `dotfiles`
    sets up the privileged account once per box), and at workspace scope it runs
    *only* the agent (so attaching a second workspace doesn't reinstall the
    admin's oh-my-zsh). A mod that never mentions the agent is pure system work
    and runs whole in whichever playbook lists it - which is what lets a
    workspace ship a `runas = ["root"]` mod (docker, a driver) onto its box.

    An empty result means the mod has nothing to do in this scope; the caller
    leaves it out of the manifest entirely."""
    if scope == SCOPE_BOX:
        return [t for t in runas if t != RUNAS_AGENT]
    if RUNAS_AGENT in runas:
        return [RUNAS_AGENT]
    return list(runas)


def _validate_ordering(specs: list, config_path, prior: Optional[dict] = None) -> None:
    """Fail fast if a mod is in the playbook without its declared dependencies
    (`requires`) ordered ahead of it. Deliberately NOT a resolver: no
    topo-sort, no auto-insert - just check the parsed headers against order.

    `prior` names mods a dependency is satisfied by wherever they sit: the ones
    an earlier playbook already ran (the box's, for a workspace run), and the
    ones this attachment's generation provides - a module is not a step in the
    run at all, and the generation that carries it is switched by
    `home-manager`, the first mod in the workspace base."""
    # First index wins on a duplicate; base always precedes workspace mods, so
    # this doesn't affect the ordering guarantee.
    index = {}
    for i, spec in enumerate(specs):
        index.setdefault(spec.name, i)
    for i, spec in enumerate(specs):
        for dep in spec.requires:
            if prior and dep in prior:
                continue
            if dep not in index:
                die(f"mod '{spec.name}' requires '{dep}', which isn't in the playbook: "
                    f"add '{dep}' to `mods` in {config_path}.")
            if index[dep] > i:
                die(f"mod '{spec.name}' requires '{dep}', but '{dep}' is listed after it "
                    f"in the playbook: move '{dep}' earlier in `mods`.")


def validate_playbooks(workspace: WorkspaceConfig) -> None:
    """Validate both playbooks' metadata: `runas` targets (in load_mod_specs)
    and `requires` ordering. Runs at playbook-resolution time (static,
    instant, before any boot/SSH).

    The box playbook is indexed first, so a workspace mod's `requires =
    ["dotfiles-packages"]` is satisfied by the box having run it - box mods are
    always "earlier" than any attachment, as is anything the attachment's
    generation provides. Validates the FULL configured playbooks:
    callers must pass the configured workspace even for selective runs (`devbox
    sync --mods=a`), since deps are about the box's cumulative state, not the
    selected subset."""
    config_path = workspace.marker.config_path
    box = resolve_playbook(box_playbook(), None, SCOPE_BOX)
    _validate_ordering(box.specs, config_path)
    attachment = resolve_playbook(workspace_playbook(workspace),
                                  workspace.marker.mods_dir)
    _validate_ordering(attachment.specs, config_path,
                       prior={s.name for s in box.specs} | set(attachment.modules))


# ---- environment context (not %TOKEN% substitution) -------------------------
def _box_context(admin_user: str, gc: GlobalConfig) -> dict:
    """The DEVBOX_* context for a box-scope run. No agent account (the driver
    reads an empty DEVBOX_AGENT_USER as "this run has no attachment") and no jj
    identity or repo, which are an attachment's business, not a machine's."""
    return {
        "DEVBOX_ADMIN_USER": admin_user,
        "DEVBOX_AGENT_USER": "",
        # The host's public key, so the driver can authorize the same key for the
        # agent accounts it creates. Public by definition - safe in the payload,
        # which carries no secrets.
        "DEVBOX_SSH_PUBKEY": gc.read_ssh_pubkey(),
    }


def _workspace_context(workspace: WorkspaceConfig, gc: GlobalConfig, admin_user: str) -> dict:
    """The DEVBOX_* provisioning context the driver exposes to every mod
    (non-secret, safe to trace). User credentials/vars are delivered separately
    as the account's [env] file (see sync.run_envvars), not in the payload.

    The checkouts themselves are NOT here - there can be any number of them, and
    a shell environment is a poor list. They travel as the `repos` payload file
    (see render_repos); this carries only their guest directories, which the
    driver needs as a set to spot a pre-split box."""
    jj_name, jj_email = resolve_jj_identity(gc)
    return {
        # Both accounts, named explicitly: a bare DEVBOX_USER would be ambiguous
        # now that a box has a privileged user and one agent per attachment.
        "DEVBOX_ADMIN_USER": admin_user,
        "DEVBOX_AGENT_USER": workspace.agent_user,
        # Which workspace owns that account. The driver records it on the box, so
        # a second workspace can be refused the account rather than silently
        # sharing a home with the first.
        "DEVBOX_WORKSPACE": workspace.workspace,
        # Which workstation attached it. Only used to note that the same
        # workspace is now being driven from somewhere else - a warning, never a
        # refusal.
        "DEVBOX_ATTACH_HOST": platform.node(),
        "DEVBOX_SSH_PUBKEY": gc.read_ssh_pubkey(),
        "DEVBOX_JJ_NAME": jj_name,
        "DEVBOX_JJ_EMAIL": jj_email,
        "DEVBOX_REPO_DIRS": ",".join(r.dir for r in workspace.repos),
    }


def render_manifest(specs: list, scope: str) -> str:
    """The payload's `manifest`: one tab-separated record per mod, in playbook
    order - `<name>\\t<runas,...>\\t<packages,...>`. Tab-separated with
    comma-joined lists so the driver can read it with plain `IFS` splitting; it
    has no YAML/JSON parser and should not grow one.

    The runas column is already narrowed to `scope` (see scoped_runas), and a mod
    with nothing to do in this scope is left out entirely - so the driver never
    has to know that scopes exist."""
    lines = []
    for spec in specs:
        runas = scoped_runas(spec.runas, scope)
        if not runas:
            continue
        lines.append(f"{spec.name}\t{','.join(runas)}\t{','.join(spec.packages)}\n")
    return "".join(lines)


def render_repos(repos: list) -> str:
    """The payload's `repos`: one tab-separated record per checkout, in config
    order - `<name>\\t<mode>\\t<dir>\\t<url>`. Same shape as the manifest, and for
    the same reason: `repo-bootstrap` reads it with plain `IFS` splitting and has
    no parser to spare.

    A file rather than a context variable because the count is unbounded. Tabs
    are safe as the separator - none of the four fields can contain one (a name
    and a directory are path components, a mode is a keyword, and a URL that
    contained a tab would not be a URL)."""
    return "".join(f"{r.name}\t{r.mode}\t{r.dir}\t{r.url or ''}\n" for r in repos)


def render_env_file(env: dict) -> str:
    """A sourceable env file: `export KEY=<shell-quoted value>` per entry. Proper
    quoting (unlike %TOKEN% text substitution) means values with spaces/quotes/%
    survive intact."""
    return "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())


# ---- the home-manager flake (the agent's declarative half) -------------------
# The `mods` bash scripts install into a home imperatively; the flake under
# NIX_DIR describes one declaratively, and `mods/home-manager.sh` switches the
# agent account to it. Both halves ride in the same payload, and a mod is in one
# or the other, never both.
#
# `workspace.nix` is to the flake what `manifest`/`repos` are to the driver: the
# rendered file that turns a shipped, static program into this attachment's. It
# carries what _workspace_context already computes, as a plain attribute set -
# NEVER a cred, because /nix/store is world-readable and shared by every
# attachment on the box (creds stay in the 0600 ~/.config/devbox/env that
# run_envvars writes).

# Nix's own escapes for a double-quoted string. `$` is escaped everywhere rather
# than only before `{`, which is legal (`\$` is `$`) and leaves no way for a
# value to open an antiquotation.
_NIX_ESCAPES = {"\\": "\\\\", '"': '\\"', "$": "\\$",
                "\n": "\\n", "\r": "\\r", "\t": "\\t"}
# An attribute name that needs no quoting: nix identifiers admit `-` and `'`,
# which covers every mod name devbox ships.
_NIX_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*\Z")
# ... except a keyword, which cannot be a bare attribute name.
_NIX_KEYWORDS = frozenset(("if", "then", "else", "assert", "with", "let", "in",
                           "rec", "inherit", "or"))


def nix_string(value: str) -> str:
    """One nix string literal, escaped. The nix equivalent of shlex.quote for
    the env file: a jj name with a quote, a backslash or a `${` in it survives
    as itself instead of becoming syntax."""
    return '"' + "".join(_NIX_ESCAPES.get(ch, ch) for ch in value) + '"'


def nix_attr_name(name: str) -> str:
    """One attribute name, bare where nix allows it and quoted where it does
    not - so a mod called `repo-bootstrap` reads naturally and one called
    anything else is still valid nix rather than a parse error."""
    if _NIX_IDENT_RE.match(name) and name not in _NIX_KEYWORDS:
        return name
    return nix_string(name)


def render_workspace_nix(context: dict, mods: dict) -> str:
    """The payload's `nix/workspace.nix`: this attachment's values as a plain
    nix attribute set, imported by the flake's options.nix.

    `mods` maps EVERY mod the flake ships a module for to whether this
    attachment's playbook resolves that name to the generation - so a module can
    always read `config.devbox.mods.<name>.enable` without first asking whether
    the attribute exists. The flag says one thing: "the generation provides this
    mod". A name the workspace never asked for is false, and so is one a bash
    `<name>.sh` answers to (the script runs and the module stays inert), which
    is what keeps a shadowed mod from being installed twice.

    Alphabetical: an attribute set is unordered, and the module files are
    discovered in sorted order, so a rendered file diffs cleanly."""
    agent = context["DEVBOX_AGENT_USER"]
    enables = [f"    {nix_attr_name(name)}.enable = "
               f"{'true' if enabled else 'false'};\n"
               for name, enabled in sorted(mods.items())]
    return (
        "# Generated by devbox (provision.render_workspace_nix) for one\n"
        "# attachment, and shipped in the provisioning payload. Editing it on\n"
        "# the box is pointless: the payload is a per-run temp directory.\n"
        "#\n"
        "# Data only, and no credentials - /nix/store is world-readable.\n"
        "{\n"
        f"  agentUser = {nix_string(agent)};\n"
        # The same home the driver creates and run_sync/run_envvars write into.
        f"  homeDirectory = {nix_string(f'/home/{agent}')};\n"
        f"  jjName = {nix_string(context['DEVBOX_JJ_NAME'])};\n"
        f"  jjEmail = {nix_string(context['DEVBOX_JJ_EMAIL'])};\n"
        + ("  mods = { };\n" if not enables
           else "  mods = {\n" + "".join(enables) + "  };\n")
        + "}\n")


def nix_payload_files(context: dict, modules: list,
                      mods_dir: Optional[Path] = None) -> list:
    """The flake directory as payload files: the flake itself, every module it
    ships (workspace > global > built-in, one file per name - see
    find_nix_module), and the rendered `workspace.nix`.

    This is where the skeleton in NIX_DIR and the modules scattered across the
    three mods directories become one evaluable flake. Each module lands at
    `nix/modules/<name>.nix` whatever directory it came from, which is what
    lets every level keep its modules beside its scripts: nix only ever sees
    this assembled layout.

    `modules` is the half of this attachment's playbook the generation provides
    (see resolve_playbook), which becomes the true half of the rendered flags;
    every other shipped module is rendered false. Both the files and the flags
    come from the same three-source lookup, so a sibling adding a
    `<mod>.nix` anywhere in it - built-in, global, or this workspace's own -
    ships it and gets its flag by writing the file; there is no second place
    to register it. `mods_dir` is a workspace's own (see config.Marker); None
    widens nothing beyond the global/built-in set (a box never calls this at
    all - see build_box_payload)."""
    if not (NIX_DIR / "flake.nix").is_file():
        die(f"devbox internal error: the home-manager flake is not at "
            f"{NIX_DIR} (broken install?)")
    names = nix_module_names(mods_dir)
    generation = set(modules)
    files = [PayloadFile(
        f"{NIX_PAYLOAD_DIR}/{NIX_WORKSPACE_FILE}",
        render_workspace_nix(context, {n: n in generation for n in names}), 0o644)]
    for path in sorted(NIX_DIR.glob("*.nix")):
        if path.name == NIX_WORKSPACE_FILE:
            continue
        files.append(PayloadFile(f"{NIX_PAYLOAD_DIR}/{path.name}",
                                 path.read_text(), 0o644))
    for name in names:
        module_path = find_nix_module(name, mods_dir)
        files.append(PayloadFile(f"{NIX_PAYLOAD_DIR}/modules/{name}.nix",
                                 module_path.read_text(), 0o644))
    return files


# The placeholder attachment `devbox nix check` evaluates against. A check has
# no workspace to take these from, and nothing in the flake may depend on their
# values anyway: a module reads `config.devbox.workspace.*` as opaque strings.
CHECK_CONTEXT = {
    "DEVBOX_AGENT_USER": "agent",
    "DEVBOX_JJ_NAME": "Nix Check",
    "DEVBOX_JJ_EMAIL": "nix-check@example.invalid",
}


def nix_check_files(mods_dir: Optional[Path] = None) -> list:
    """The payload flake as `devbox nix check` evaluates it: every module any
    mods directory provides, with EVERY flag true.

    All-on is deliberate and is not what any attachment looks like. A module's
    body sits inside `lib.mkIf config.devbox.mods.<name>.enable`, whose content
    nix never forces while the flag is false - so a check that mirrored a real
    playbook would evaluate only the handful of modules that playbook happens
    to name, and a typo in any other would pass. Turning everything on is what
    makes the check cover every module, and it also surfaces two modules
    claiming the same file, which a real attachment would only hit later."""
    return nix_payload_files(CHECK_CONTEXT, nix_module_names(mods_dir), mods_dir)


# ---- payload assembly -------------------------------------------------------
def _assemble(specs: list, context: dict, scope: str, repos: list = ()) -> list:
    """The payload files for one run: the driver, the scope-narrowed manifest,
    the context, the workspace's checkouts, and every mod that has something to
    do in this scope."""
    if not DRIVER_PATH.is_file():
        die(f"devbox internal error: provisioning driver not found at "
            f"{DRIVER_PATH} (broken install?)")
    manifest = render_manifest(specs, scope)
    payload = [
        PayloadFile("provision-driver.sh", DRIVER_PATH.read_text(), 0o755),
        PayloadFile("manifest", manifest, 0o644),
        PayloadFile("context.env", render_env_file(context), 0o644),
        PayloadFile("repos", render_repos(repos), 0o644),
    ]
    for spec in specs:
        if scoped_runas(spec.runas, scope):
            payload.append(PayloadFile(f"mods/{spec.name}.sh", spec.path.read_text(), 0o755))
    return payload


def build_box_payload(admin_user: str, gc: GlobalConfig) -> list:
    """The provisioning payload for a box itself: the fixed box playbook, run as
    the privileged account (and root). No workspace is involved, so no agent
    account is created and no jj identity is required. Takes just the account
    rather than a whole BoxSpec, because that is all a box run needs - the rest
    of a box's shape only matters at launch."""
    box = resolve_playbook(box_playbook(), None, SCOPE_BOX)
    return _assemble(box.specs, _box_context(admin_user, gc), SCOPE_BOX)


def build_payload(workspace: WorkspaceConfig, gc: GlobalConfig, admin_user: str) -> list:
    """The full provisioning payload for one attachment (driver + manifest +
    context + repos + resolved mods + the home-manager flake), as PayloadFiles.
    Tarred and run over SSH by both `devbox create` and `devbox sync --mods` -
    the identical set either way.

    The flake travels with an attachment and not with a box, because
    home-manager describes one account's home: a box run has no account to
    switch, and the privileged one is deliberately left on apt. Which half of
    the payload a playbook name lands in - a `mods/<name>.sh` the driver runs or
    a flag on a module the generation carries - is resolve_playbook's decision.

    Validates BOTH configured playbooks' metadata (`runas` targets and
    `requires` ordering) first. This is the single choke point: build_payload
    always builds the whole manifest (even for a selective `devbox sync
    --mods=a`, which validates here and then runs only the selected subset), so
    deps are checked against the box's cumulative state, not the subset."""
    validate_playbooks(workspace)
    attachment = resolve_playbook(workspace_playbook(workspace),
                                  workspace.marker.mods_dir)
    context = _workspace_context(workspace, gc, admin_user)
    payload = _assemble(attachment.specs, context, SCOPE_WORKSPACE, workspace.repos)
    payload.extend(nix_payload_files(context, attachment.modules,
                                     workspace.marker.mods_dir))
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
