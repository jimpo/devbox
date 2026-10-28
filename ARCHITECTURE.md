# Architecture

How devbox works and why. Usage is in [README.md](README.md).

## Boxes and workspaces

A **box** is a machine; a **workspace** is the content that attaches to it, as
one unprivileged account. The VM provides the security boundary; accounts mostly
provide organization.

**No box config file.** A box's resources are fixed at create time and readable
from the provider afterwards, so a stored spec could only disagree with the
machine. `config.resolve_box_spec` assembles one from `devbox box create` flags
plus the global `[defaults]` and never persists it. Hardware keys in a workspace
config are a hard error naming the flag to use instead. The one fact devbox
stamps on the instance (libvirt `<metadata>` `<user>`, AWS tag
`devbox:admin-user`) is the privileged username, because the host needs it to
build the SSH alias before it can log in. An unstamped box falls back to the
host's `$USER`.

**The privileged account defaults to `admin`**, not the host's `$USER`: it is the
machine's generic rescue login, not a person's. `admin` stays in
`RESERVED_USERNAMES` (agent-side), so a workspace called `admin` is refused.
`agent_user` is validated against `USERNAME_RE`/`RESERVED_USERNAMES` at config
load; "differs from the box's privileged account" can only be checked at attach.

**Names route to providers.** A canonical box name is `<provider>-<name>` for
cloud providers and bare for libvirt, so every command finds the backend by
parsing the name, with no API call. The AWS-side name drops the redundant prefix;
the `devbox:name` tag keeps the canonical one.

**Two marker spellings, one `Marker`.** `find_workspace` returns the root, config
path, mods dir and the `root_is_repo` bit together, so no caller re-derives
paths. `root_is_repo` supplies a nameless `[[repos]]` entry's meaning ("the root
itself", legal only under `.devbox/`) and decides whether a bare-relative
`[[sync]]` path is meaningful: it lines up on both sides only when the root holds
the repos. Repos are listed, never discovered by globbing for `.git`.

## First boot

`initscript.py` renders `devbox/init-script.sh` (literal
`%DEVBOX_USER%`/`%DEVBOX_SSH_PUBKEY%`/`%DEVBOX_HOSTNAME%`; `{{…}}` would collide
with cloud-init's jinja) and every provider gets it as user-data. A shell script
is the only user-data all three accept: cloud-init runs it on libvirt (NoCloud
seed) and EC2, and Lightsail runs it natively but does not understand
`#cloud-config`. It takes a `BoxSpec`, not a workspace. As root, it:

1. creates the privileged user with NOPASSWD sudo and your SSH key — first, so
   SSH never depends on apt succeeding;
2. sets the hostname (plus `preserve_hostname`);
3. points `/etc/environment` at `BASH_ENV=/etc/devbox/bash_env`, a shared stub
   that sources `$HOME/.bash_env` (pam_env doesn't expand `$HOME`, and one
   system-wide path can't be per-user);
4. installs baseline apt packages (the GitHub CLI apt repo first, since `gh`
   isn't in every Ubuntu archive);
5. leaves the privileged account's shell as `/bin/bash`.

It creates **no** workspace account: user-data runs once, so such an account
could never reach an existing box. Changing it needs `box destroy` + `box create`.

## Provisioning

Everything else runs **over SSH after boot**, as one path for a fresh box and a
reconverge: devbox tars the driver, the resolved `mods/*.sh`, `context.env`, a
manifest, a `repos` file and (for a workspace) the `nix/` flake, pipes it over the
`devbox-<box>-admin` alias, extracts it to a temp dir and runs
`provision-driver.sh` as the privileged user. `box create` runs the box playbook;
`create` and `sync --mods` run the workspace playbook. The host must stay
connected; a dropped run is fixed by re-running `devbox sync --mods`.

**Host-side resolution.** `provision.py` parses each mod's `# /// mod` block
(`tomllib`; unknown keys, bad values, duplicate or mislabeled fences are errors),
validates `requires` order (box playbook indexed first, so a workspace mod may
require a box mod; validation only, never reordering), and narrows each mod's
`runas` to the run's scope (`scoped_runas`) before writing the manifest. The
driver never reads a mod file and never reasons about scope.

**The driver's preamble** runs before any mod, every run, and can't be shadowed
or skipped:
- `ensure_admin` wires the privileged account's `~/.bash_env`.
- `ensure_agent` (workspace runs only; an empty `DEVBOX_AGENT_USER` means a box
  run) creates the account if absent — no supplementary groups, `0750` home,
  locked password, your key — enables lingering so its systemd user services
  outlive SSH sessions (a note, not a failure, without `loginctl`), and records
  ownership in `/etc/devbox/workspaces/<agent_user>`. It **refuses** an account
  owned by another workspace, one with no record (unless `--adopt`), or one that
  has acquired sudo or a root-equivalent group. A legacy record under
  `/etc/devbox/projects/` (`project=`) is honoured and retired.
- `install_bash_env` writes each account's `~/.bash_env`, the only thing that puts
  nix on a **non-interactive** PATH (`ssh box '<cmd>'`, Claude Code's Bash tool):
  the store's profile script, `hm-session-vars.sh`, and `~/.nix-profile/bin`
  ahead of `~/.local/bin`, so a stale binary from a since-converted mod can't
  shadow the generation.

Then for each mod: apt-install its `packages` as the privileged user (index
refreshed once; `DPkg::Lock::Timeout=600`), and run the body via `sudo -u` per
target. Context and creds are re-sourced inside the target shell, never passed
on `sudo`'s argv. The run stops at the first failure, naming the mod and account.
Logs are per attachment so concurrent workspaces don't clobber each other, but
nothing serializes two full runs on one box.

**Mod bodies have no sudo**, and the payload has no secrets. Both follow from the
driver dropping to the agent: the payload has to be readable by that account.
Each mod sources the `~/.config/devbox/env` of the account that owns its creds
(the target's, or the privileged account's for a `root` mod).

**Box playbook**: `box-packages` (an apt list, no body: login `zsh`, the
privileged account's `vim`, `rsync`, build basics) and `nix`, the multi-user
store pinned by installer version. The store is the machine's, shared by every
attachment. `trusted-users` holds only the privileged account, since a trusted
user could push paths another workspace's agent would run. There's no GC timer;
the daemon's `min-free`/`max-free` reclaim space under pressure.

## Mods: two halves

A playbook name resolves (`resolve_playbook` → `Playbook(specs, modules)`) by file
presence in the three `mods_dirs` (workspace > global > built-in):
`<name>.sh` puts it in `specs` (a manifest line), `<name>.nix` in `modules` (part
of the generation). A name can have both. Each suffix shadows only its own suffix
at every level. There is no registry to keep in step, and `find_mod_script` and
`find_nix_module` differ only in the suffix.

`home-manager` is the first workspace mod, so the generation is switched before
any script runs and a script can use what a module put on PATH (`repo-bootstrap`
runs `jj` from `jj.nix` through `github-auth.nix`'s credential helper). So
`_validate_ordering` treats a module-only dependency as satisfied wherever it
sits, but checks a dependency that has a script by its position.

**Converting a mod** from script to module: write `<name>.nix`, delete
`<name>.sh` (a leftover script still runs beside the module), leave the playbook
alone. `flake.nix`'s conventions block spells out the recipe. A retired name goes
in `RETIRED_MODS` so a config still naming it gets an error saying where it went.

**Agent CLIs stay scripts** (`claude-code`, `codex`): their installers put a
self-updating binary in the account's own `~/.local/bin`, which a read-only store
path pinned to one nixpkgs can't do. Nothing in the generation provides either
binary, so the PATH order has nothing to shadow.

## The home-manager flake

`devbox/nix/` is a **skeleton**, not a working flake: `flake.nix`, `options.nix`,
`zshenv.nix`. `nix_payload_files` assembles a real one per attachment: the
skeleton, a rendered `workspace.nix`, and one `modules/<name>.nix` per resolved
module copied from whichever mods dir it came from. That assembled layout is all
nix sees, so it can't be evaluated in place (`devbox nix check` assembles one).

- `flake.nix` `readDir`s `./modules` rather than importing a list: adding a
  module is one file. Every `.nix` in a mods dir is therefore a module.
- `options.nix` declares `devbox.workspace.*` and free-form
  `devbox.mods.<name>.enable`, and is the only importer of `workspace.nix`.
- `render_workspace_nix` writes agent user, home, jj identity and **one flag per
  shipped module**: true when this attachment's playbook names it, false otherwise
  (a missing attribute is an evaluation error, not a false). Strings are escaped
  for nix.
- nixpkgs and home-manager are pinned to exact revs; `nixpkgs-unstable` is a
  separate pin used only for `cloudflared`. `configenv` (dotfiles) is a
  **non-flake** input on a rolling branch, so there is **no `flake.lock`**: a lock
  would freeze configenv, and the exact revs make nix's on-box lock deterministic
  for the rest. `home-manager.sh` passes `--refresh` to re-resolve the branch.
- `zshenv.nix` is always in the generation. It enables `programs.zsh` (the one
  writer of `~/.zshenv` — a second `home.file` claiming it would fail the build),
  sources the nix profile script and the 0600 env file from `envExtra`, and adds
  `~/.local/bin` via `home.sessionPath`. `envExtra` lands after home-manager's
  block, so `~/.nix-profile/bin` comes first.
- **No credential ever enters a nix expression**: the store is world-readable and
  shared. A module that needs a secret names the env file's path and reads it at
  run time (`cloudflared` sources it in its unit; systemd's `EnvironmentFile=`
  would drop devbox's `export KEY=…` lines).
- **Agent-only.** The privileged account has no generation, stock bash and
  unconfigured vim, so `devbox box ssh` works whatever state any generation is in.

## Credentials and sync

`run_envvars` pipes the `[env]` map over the admin alias (never in argv or logs)
and rewrites each account's 0600 `~/.config/devbox/env` in full, so removed keys
disappear. `owner` picks the one account an entry goes to. The workspace map is
global `[env]`, then workspace `[env]`, then `secrets.env` (`_merge_secrets_env`;
an existing entry keeps its `owner`). The privileged account's `~/.zshenv` is
devbox-owned; an agent's is its generation's.

`sync.run_sync` runs one rsync per mirror unit over the workspace's own alias.
Every rule carries an anchor (`ANCHOR_HOME`/`ANCHOR_WORKSPACE`/`ANCHOR_REPO`) and
all three put the box end under the agent's home. Only `$HOME`/`~` expand, both
resolved offline per side.

**SSH aliases**: one stanza per file under `~/.config/devbox/ssh/`, regenerated on
every call because addresses change across stop/up. One shared file per box would
let each workspace's refresh overwrite another's account. Each box gets its own
known_hosts, truncated when that box is created.

## Accounts and isolation

Homes are `0750` with a per-user group; env files are `0600`. The privileged side
can read everything and is the trusted side. What sharing a box costs:

- **One apt namespace.** A `packages` entry or root mod from one workspace changes
  what every workspace builds against. Per-workspace toolchains belong in `$HOME`.
- **Every attached workspace's tokens are on one machine.** A kernel escape yields
  all of them. Prefer repo-scoped PATs in a workspace's own `[env]`.
- **`/proc/<pid>/cmdline` is world-readable** (`environ` is not). Mounting `/proc`
  with `hidepid=2` from a root mod would fix it; none ships.
- **Shared CPU, RAM and disk.** One agent can ENOSPC the rest.
- **`box destroy` takes everyone's uncommitted work**; the workspace-level
  `destroy` refuses when anyone else is attached.
- **Duplication by design**: each account has its own checkouts. `/nix/store` is
  the exception, shared machine-wide.

devbox attaches **no** instance profile on AWS. If one is ever added, block IMDS
(`169.254.169.254`) for agent accounts in the same change, because the agent
reaches it as easily as root.

## Detach

`devbox detach` ships no payload, so `cli._detach_script` is a host-built root
command, not a driver mode. It removes the ownership marker, the authorized key,
the creds, and the account's nix profile, generation links and home-manager GC
roots (plus the per-user dirs under `/nix/var/nix`). A generation is a GC root, so
a box that has hosted many workspaces would otherwise keep all their closures. The
files stay (`devbox create` re-switches); `--purge` removes the home. It runs **no**
garbage collection: that's the store's min-free/max-free policy, and a sweep would
discard paths the box's other workspaces are about to reuse.

## Code layout

- `config.py` — `GlobalConfig`, `WorkspaceConfig`, `Marker`/`find_workspace`,
  `[[repos]]`, `secrets.env` merge, `resolve_instance`/`parse_instance_name`,
  `resolve_box_spec`.
- `provision.py` — mod resolution (both halves), the two playbooks, metadata
  parsing, `scoped_runas` + `render_manifest`, `render_repos`, context/env
  rendering, `render_workspace_nix`, payload assembly.
- `provision-driver.sh` — the on-box driver (above).
- `init-script.sh` + `initscript.py` — first boot.
- `mods/` — built-in mods, both halves in one directory. `nix/` — the flake
  skeleton.
- `sync.py` — `run_sync` and `run_envvars`.
- `providers/` — the backend seam. `base.py` (`Provider`, `InstanceSpec`,
  `Instance`); `libvirt.py`, `ec2.py`, `lightsail.py` (the `aws` provider). Only
  `create`/`start`/`stop`/`destroy`/`state`/`address`/`list` are per-backend;
  everything above is shared. `awsauth.py`'s `make_client` checks credentials
  before the first API call and offers `aws sso login`. `AWS_PROVIDERS` are used
  only when `[aws]` is configured.
- `cli.py` — argparse and handlers. `cmd_box_*` act on a machine by name;
  everything else on this workspace's account. `_run` turns AWS auth failures
  into one-line errors; `_list_boxes` reports an unreachable provider as a
  warning rather than failing the whole listing; `_select_repo` picks a checkout.
- `__init__.py` — a flat `devbox.X` re-export for tests.
