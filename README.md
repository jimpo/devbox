# devbox

A tool for launching development virtual machines and attaching workspaces to
them. Its primary use cases are sandboxed agentic development, and
cross-platform development and benchmarking on cloud VMs.

devbox gives you an easy-to-use CLI for creating, managing, and interacting with
those machines, and ships with an agent skill.

It currently supports three VM providers: **libvirt** (local VMs), **ec2**
(AWS EC2), and **aws** (AWS Lightsail — flat-rate bundles, simpler than EC2).

## Basics & Architecture

Every devbox starts from the latest Ubuntu LTS as its base image — currently
Ubuntu 26.04 "Resolute Raccoon" — brought to life on first boot by a single
bash **init script** handed to the instance as user-data (just the privileged
user, baseline packages, `BASH_ENV` wiring, and the hostname). Everything else — jj, dotfiles, Claude Code, the repo,
a workspace's toolchain — is provisioned by composable, idempotent **mods** run
**over SSH** after boot, so a box can be reconverged in place (`devbox sync
--mods`) instead of rebuilt.

### A box is not a workspace

A **box** is a VM. A **workspace** is a directory on your host holding one or
more repos, marked by a devbox config file. Several workspaces can live on one
box, each in its own unprivileged account — because the VM is what provides
*security* isolation, and the accounts are mostly for *organization*.

| | **box** | **workspace** |
| --- | --- | --- |
| is | a machine: provider, resources, image, hostname | content: an account, its checkouts, creds, mods |
| named | `dev`, `ec2-dev` (canonical instance name) | the marker directory's basename |
| owns | the privileged account, system packages | its `agent_user`, `[[repos]]`, `mods`, `[env]`, `[[sync]]` |
| created by | `devbox box create dev [flags]` | `devbox create`, which attaches it to its box |
| configured by | **command-line flags only** — there is no box config file | `devbox.toml` or `.devbox/config.toml` |
| recorded in | the instance's own tags / libvirt `<metadata>` | `/etc/devbox/workspaces/<agent_user>` on the box |

A workspace says which box it lives on with `box = "dev"`. It defaults to the
workspace's own name, so a workspace that never mentions a box gets one to itself —
which is why the single-box case still needs no configuration at all.

### Where the marker goes

A workspace is marked one of two ways, and which one you use says what shape the
directory has. Both hold the same keys; only the location differs.

```
~/src/devbox/              ~/src/acme/
  .devbox/                   devbox.toml        <- visible: nothing to hide from
    config.toml              mods/rust.sh
    mods/rust.sh             frontend/          <- a repo
  devbox/                    backend/           <- a repo
  README.md                  shared-protos/     <- a repo
```

**`<repo>/.devbox/config.toml`** — the workspace *is* one repo. The marker hides
inside that repo's tree, so git-exclude it (`.git/info/exclude`).

**`<root>/devbox.toml`** — the workspace *holds* repos, as children of the root.
There is no repo to hide the marker from, so it is a plain visible file, and the
mods sit beside it in `<root>/mods/`.

devbox walks up from the working directory looking for either, nearest level
first. A directory holding both is an error: which shape the workspace has
decides every repo's host path, and that is not a coin to toss.

### A workspace holds repos

Every checkout in one workspace lives in one account, and therefore shares one
set of git/jj credentials. That is the point: a change spanning a client and its
server is one agent's job, not two accounts'.

```toml
# ~/src/acme/devbox.toml
box = "dev"

[[repos]]
name = "frontend"     # host ~/src/acme/frontend  ->  box ~/frontend
mode = "push"

[[repos]]
name = "backend"
mode = "push"

[[repos]]
name = "shared-protos"
mode = "clone"        # no host copy needed; the box clones it
url  = "git@github.com:acme/shared-protos.git"
```

One `name` is usually the whole of an entry: it addresses the repo (in
`[[repos.sync]]` and `devbox jj fetch`), and doubles as its host path under the
root and its directory in the agent's home. Override either with `host` / `dir`.

Under a `.devbox/` marker the root is itself the repo, so an entry needs no name
at all:

```toml
# ~/src/devbox/.devbox/config.toml
box = "dev"

[[repos]]
mode = "push"         # host ~/src/devbox  ->  box ~/devbox
```

Repos are listed, never discovered: globbing the root for `.git` would put an
unrelated clone on the box the moment you made one. A workspace with nothing to
check out simply lists no repos.

### Coming from `[repo]` and "projects"

What devbox called a *project* is now a *workspace*, and it holds a list of
checkouts instead of one. Existing config files need three edits, each of which
devbox will name if you forget it:

| was | now |
| --- | --- |
| `project = "x"` | `workspace = "x"` |
| `[repo]` with `mode`/`dir`/`url` | a `[[repos]]` entry; `dir` still names the box-side directory |
| `[[repo.sync]]` | `[[repos.sync]]`, nested under its `[[repos]]` entry |
| `[repo] mode = "none"` | no `[[repos]]` entries at all |

Marker files stay where they are — `.devbox/config.toml` is still the shape for a
workspace that is one repo. A live box needs nothing: it records which workspace
owns each account under `/etc/devbox/workspaces/`, and a box last attached by an
older devbox has that record under `/etc/devbox/projects/`, which devbox still
reads and retires on the next `devbox create`.

**No box config file.** A box's resources are fixed the moment it is created and
readable from the provider afterwards, so a persisted spec could only ever
disagree with the machine. Box flags therefore live on the command line and
nowhere else. The one fact devbox does record on the instance is the **privileged
username**, because it needs that to build the box's SSH alias *before* it can
log in and ask. The cost of this is real but small: recreating a destroyed box
means retyping the flags.

### Accounts on a box

Every box has one privileged user and one unprivileged account per attached
workspace. AI agents run as the latter:

| | **privileged** (`--user`, default `$USER`) | **agent** (`agent_user`, default = the workspace name) |
| --- | --- | --- |
| how many | one per box | one per attached workspace |
| sudo | NOPASSWD `ALL` | **none**, and in no group that grants it |
| home | `/home/<user>`, `0750` | `/home/<agent_user>`, `0750`, own group |
| holds | the provisioning payload, the provision logs | that workspace's checkout, `~/.claude`, `~/.local/bin`, its `[env]` creds |
| SSH | `devbox box ssh <box>` | `devbox ssh` (from the workspace) |
| created by | the init script, at first boot | the provisioning driver, at attach, so it reconverges |

**What this buys, and what it doesn't.** A sudoless agent still has full network
egress and still holds `GITHUB_TOKEN` / `LINEAR_API_KEY` — which for most threat
models *are* the crown jewels. This is containment of **system integrity**, not
of **credential exfiltration**. What it does gain: the agent can't tamper with
the provisioner or the system, can't read the privileged user's home *or any
other workspace's*, can't persist outside its own, and has a crisply defined blast
radius.

**What sharing a box costs.** Worth knowing before you put two workspaces on one
machine:

- **One apt namespace.** A `packages` entry or a `runas = ["root"]` mod from one
  workspace upgrades a library every other workspace builds against. Per-workspace
  toolchains belong in `$HOME` (rustup, uv, nvm — which is already how every
  base mod but `dotfiles-packages` works). devbox warns when a workspace run
  includes a privileged mod, since that writes state the whole box shares.
- **`devbox box destroy` takes everyone's uncommitted work.** The confirm prompt
  names the attached workspaces, and the workspace-level `devbox destroy` refuses
  outright when anything else is on the box.
- **Every attached workspace's tokens are on one machine.** The 0600 per-account
  env files keep agents out of each other's, but a kernel escape from any one of
  them yields all of them. Prefer repo-scoped PATs in a workspace's own `[env]`,
  and note that a *global* `[env]` `GITHUB_TOKEN` now sits on a box shared with
  unrelated workspaces.
- **`/proc/<pid>/cmdline` is world-readable**, so one agent can read another's
  argv. (`environ` is 0400, so the env file itself stays private.) A
  `runas = ["root"]` box mod mounting `/proc` with `hidepid=2` is the fix if that
  matters to you; devbox doesn't ship one.
- **Shared CPU, RAM and disk.** N agents running `cargo build` contend, and one
  can ENOSPC the rest. Box sizing is now an N-workspace decision.
- **Duplication, deliberately.** Each account gets its own oh-my-zsh, jj binary,
  Claude Code install and vim plugins — a few minutes and a few hundred MB per
  attach, accepted in exchange for accounts that don't share state.
- **One provisioning run per box at a time.** Concurrent attaches share apt's
  lock (devbox sets `DPkg::Lock::Timeout=600`) and the ownership markers are
  written atomically, but nothing serializes two full runs.

> **Never grant the agent partial sudo.** `NOPASSWD: /usr/bin/apt-get` is
> equivalent to full root — `apt-get install ./evil.deb` runs maintainer scripts
> as root, and `-o APT::Update::Pre-Invoke=…` runs arbitrary commands. The same
> goes for `sudo systemctl`, `sudo pip`, or membership in `docker`/`lxd`. If any
> of those get added, the isolation is decorative and should be described that
> way. The provisioning driver asserts this on every run and refuses to continue
> if the agent has picked up sudo or a root-equivalent group.

The trade-off is real and deliberate: **the agent cannot install system
packages.** Where it used to `sudo apt-get install` its way out of a missing
build dependency mid-task, it now needs a `packages` entry on a mod and a
`devbox sync --mods`. First boot installs `build-essential` and `pkg-config` to
cover the common case.

**Attaching to an existing box.** An account that already exists but has no
ownership record — a box built before devbox tracked attachments, or an account
somebody made by hand — is **refused**, rather than silently moved into. Claim it
once with `devbox create --adopt`. An account that belongs to a *different*
workspace is refused outright; pick another `agent_user`, or detach that workspace.

Once a box is up, the way you work with it —
`ssh`/`list`/`jj fetch`/`sync` — is backend-independent; only
`create`/`up`/`stop`/`destroy` and address discovery are provider-specific (a
thin `Provider` seam). All boxes share one cached base image (libvirt) and one
global config, and every attachment runs the same base playbook of idempotent
"mods" (jj, dotfiles, Claude Code, GitHub auth, repo bootstrap) that devbox owns;
each workspace appends only what's different via a `mods = [...]` list. `devbox`
finds "which box the workspace in the current working directory lives on" the way
`git`/`direnv`/`mise` locate their own markers.

## Example Workflow

Mark a directory as a devbox workspace and say how to get the code. A minimal
local (libvirt) workspace, marked inside the one repo it holds:

```toml
# ~/src/my-workspace/.devbox/config.toml
[[repos]]
mode = "push"    # the box gets an empty repo ready to receive `jj git push`
```

Then, from anywhere inside the workspace:

```bash
devbox create      # boot a box named after the workspace (base image cached),
                   #   then attach: create its account, inject creds, run the
                   #   mods, push [[sync]] files — blocks until ready
devbox ssh         # log into the sandbox as this workspace's account
```

Work in the sandbox (e.g. let an agent make changes), bookmark what should be
visible to the host, and pull it back:

```bash
# in the sandbox:
jj bookmark create wip -r @
# on the host:
devbox jj fetch                # fetch the sandbox's bookmarked work
jj new wip@devbox-my-workspace   # check it out locally
```

`devbox stop` shuts the box down (keeping its disk); `devbox destroy` tears it
down entirely.

**Sharing one box between workspaces.** Create the box once, then point each
workspace at it:

```bash
devbox box create dev --vcpus 8 --memory 32768    # or --provider ec2/aws
# in each workspace's marker file:  box = "dev"
cd ~/src/binius && devbox create                  # attaches as user `binius`
cd ~/src/web    && devbox create                  # attaches as user `web`
devbox box list --attached                        # who is on which box
```

Each workspace gets its own account, checkouts, creds and SSH alias on the shared
machine. `devbox detach` gives an account up again.

**Several repos in one workspace.** Put the marker beside them instead of inside
one of them, and list them:

```bash
mkdir ~/src/acme && cd ~/src/acme
git clone git@github.com:acme/frontend.git
git clone git@github.com:acme/backend.git
$EDITOR devbox.toml        # [[repos]] name = "frontend" / "backend", mode = "push"
devbox create              # one account on the box, holding both checkouts
devbox jj fetch            # from inside either one: fetches that one
```

Both checkouts share the account, and so share its GitHub token and jj identity —
which is what makes a change spanning them a single agent's job.

## Usage

**Box commands** act on a machine and take its name.

| Command | Behavior |
|---|---|
| `devbox box create NAME [flags]` | Launch a box: bare-named for libvirt, `<provider>-NAME` with `--provider ec2\|aws`. Renders the init script and first-boots the instance with it as user-data (libvirt NoCloud seed / EC2 `UserData` / Lightsail `userData`), stamps it (libvirt `<metadata>` / AWS tags), then runs the **box playbook**. Idempotent: an existing box is left alone. Errors if the name is taken on another provider (global-uniqueness). Flags: `--provider --user --vcpus --memory --disk --instance-type --arch --ami --spot/--on-demand --bundle --blueprint --zone`. |
| `devbox box list [--attached]` | List devbox-managed boxes — name, provider, state, resources, uptime (cloud rows show the EC2 instance type / Lightsail bundle and a `($)` cost flag while running). `--attached` adds a WORKSPACES column, read from each box (one SSH per row, hence opt-in). Global (doesn't need a workspace); unions libvirt (this host, by `<metadata>` stamp) + EC2 + Lightsail (account-wide, by `devbox:managed` tag). `devbox list` is the same command. |
| `devbox box up NAME` | Start an already-defined, shut-off box. Errors if undefined — run `box create` first. |
| `devbox box stop NAME` | Gracefully shut down (ACPI) a running box; says so when more than one workspace is attached. |
| `devbox box ssh NAME [-- args...]` | `ssh` in as the **privileged user** — the box's only sudo shell, for debugging the machine itself. |
| `devbox box status NAME` | SSH in as the privileged user and show `cloud-init status --long` plus the tail of the box's own `~/provision.log`. |
| `devbox box sync --mods[=a,b,c]` | Reconverge the **box playbook** (system state and the privileged account) without touching any attachment. |
| `devbox box destroy NAME` | Tear down the box and its disk (the cached base image is left alone). The confirm prompt names every attached workspace, since their checkouts go with it. |

**Workspace commands** act on this workspace's account on its box, and take no name.

| Command | Behavior |
|---|---|
| `devbox create` | Ensure the workspace's box exists, then **attach**: create its unprivileged account, inject the `[env]` creds, run the workspace playbook, push `[[sync]]` files. Idempotent — re-running is a reconverge. Boots the box itself when the workspace owns it (i.e. no `box =` key) or when box-create flags are passed; otherwise says to run `devbox box create` first. `--adopt` claims an account devbox has no record of. |
| `devbox ssh [-- args...]` | `ssh` in as **this workspace's** unprivileged account — where its checkout and tooling live. (The box's sudo shell is `devbox box ssh <box>`.) |
| `devbox status` | cloud-init progress plus the tail of this workspace's `~/provision-<agent_user>.log`. |
| `devbox ip` | Print the box's IPv4. |
| `devbox sync --envvars` | (Re-)write the devbox-managed 0600 `[env]` files — scoped creds/vars (`GITHUB_TOKEN`, API keys) into the shell env of the account each one's `owner` names. Full rewrite each run (drops keys removed from config). See "Credentials" below. |
| `devbox sync --mods[=a,b,c]` | (Re-)run this workspace's **mods** over SSH — reconverge (add a plugin, re-apply a rust mod) without destroy+create. No list runs the whole workspace playbook; a comma-separated list runs just those (fast iteration). |
| `devbox sync --files` | Push host-local, non-VCS files (`[[sync]]`/`[[repos.sync]]`) onto the box via rsync — agent skills, git-excluded workspace files. Re-runnable + incremental. See "Host→box file sync" below. |
| `devbox jj fetch [REPO]` | jj-fetch one checkout's bookmarked work into your local repo: refreshes the SSH alias (address via the provider), ensures the jj remote, and runs `jj git fetch`. Which checkout: the named one, else the one the working directory is inside, else the workspace's only one. See "Exchanging commits" below. |
| `devbox host` | Refresh the SSH aliases used for `jj fetch` (below); prints this workspace's alias, and names the admin one. |
| `devbox herdr [-- args]` | Launch a herdr session on the box as this workspace's agent user — refreshes the alias, then `herdr --remote <alias>`. Replaces `herdr --remote $(devbox host)`. Trailing args pass through (e.g. `--session NAME`). herdr runs on the **host**, so it has to be installed here; the box needs nothing. |
| `devbox detach [--purge]` | Give up this workspace's account: drop its ownership record, its authorized key and its creds, and lock it. The home — and any uncommitted work in it — survives, so `devbox create` re-attaches instantly. `--purge` terminates the account's sessions and `userdel -r`s it, behind the same confirmation as `destroy`. |
| `devbox destroy` | Tear down the whole box — **refused** when any other workspace is attached, pointing at `detach` / `box destroy` instead. |

`devbox sync` requires at least one of `--envvars`/`--mods`/`--files` (any
combination). They run in that order — env before mods (a mod may need a cred,
e.g. a private clone), files last (dsts can depend on the repo being cloned).
It's the same steps `devbox create` runs when attaching.

Each box gets a `devbox-<box>-admin` SSH alias for the privileged user
(`--envvars` and `--mods`, both of which need sudo), and **one alias per
attachment** for the workspace's own account: `devbox-<box>-<workspace>`, which
collapses to plain `devbox-<box>` when the workspace owns the box — so the
single-workspace case keeps the alias every existing jj remote already carries.
Each lives alone in its own file under `~/.config/devbox/ssh/`.

Every subcommand accepts `-C/--workspace-dir PATH` and `--box NAME` (both
before the subcommand, so `ssh`'s trailing args don't swallow them) — `-C`
resolves a workspace other than the current directory; `--box` targets a specific
box instead of the workspace's own. `--name` is accepted as an alias for `--box`.

**Changing the first-boot init script (the packages/login user/`BASH_ENV` in
devbox's `init-script.sh`) only takes effect via `box destroy` + `box create`,
never `up`** — user-data runs once, on first boot (on libvirt, cloud-init's
NoCloud seed ISO is only attached then), so an already-created box never re-runs
it. The
provisioning **mods**, by contrast, reconverge in place: `devbox sync --mods`
re-delivers the driver + mods + context over SSH and re-runs them on a live box,
so edits to a mod (or to the context it consumes — jj identity, GitHub token,
`[[repos]]`, a workspace's `mods` list) apply without a rebuild. So does
`agent_user`: the account is the driver's to create, not first boot's, which is
why attaching a second workspace needs no rebuild either.

### Workspace & box resolution

Workspace subcommands first find the **workspace** (box subcommands and `list` are
global and skip this):

1. Start at `-C PATH` if given, else the current directory.
2. Walk upward (like `git` searching for `.git`) looking for either marker
   spelling — `devbox.toml` or `.devbox/config.toml` — at each directory level.
3. The first directory that has one wins. A directory with **both** is an error:
   the spelling decides the workspace's shape, so it must be unambiguous.
4. No marker found up to `/` → error telling you to pass `-C` or mark a
   workspace.

Then they resolve **which box**: an explicit `--box` wins, then the workspace's
`box` key, then — for a workspace that has never mentioned one — its own name.
Daily use never needs `--box`.

## Requirements

- For the **libvirt** provider: Debian/Ubuntu/Pop!_OS with libvirt + qemu:
  ```
  sudo apt install virtinst libvirt-daemon-system qemu-system-x86 ubuntu-cloudimage-keyring
  ```
  Run as a user in the `libvirt` group (so `qemu:///system` works without
  sudo), or invoke `devbox` with sudo. `ubuntu-cloudimage-keyring` provides
  Canonical's signing key at `/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg`,
  used by `devbox create` to verify the base image's checksum (see below).
- For the **ec2** and **aws** (Lightsail) providers (optional): configured AWS
  credentials (e.g. a named profile) with EC2 / Lightsail permissions. `boto3`
  ships as a devbox dependency; the `[aws]` global-config section (the shared
  AWS account creds) enables both providers.
- [`uv`](https://docs.astral.sh/uv/) — manages devbox's install and runtime
  environment (its one dependency: `boto3`, for the AWS providers).
- `jj` (jujutsu) and `rsync` on the host — for `devbox jj fetch` and `devbox
  sync` respectively (both also shipped in the box's base packages). Skip if you
  use neither.

## Install

```bash
uv tool install --editable .
```

This installs a `devbox` command into `~/.local/bin` (must be on `$PATH`),
backed by a `uv`-managed virtual environment under
`~/.local/share/uv/tools/devbox`. `--editable` means edits to the `devbox`
package take effect immediately, without reinstalling. To update after pulling new
commits, nothing further is needed (editable install); to upgrade the
pinned version metadata or dependencies, rerun `uv tool install --editable .
--reinstall`. Uninstall with `uv tool uninstall devbox`.

## One-time host setup

1. **Global config** at `~/.config/devbox/config.toml` (optional — every key
   has a built-in default; only set what you want to override):
   ```toml
   libvirt_uri = "qemu:///system"
   network = "default"
   cache_dir = "/var/tmp/devbox"       # base image cache; NOT $HOME (a 0700
                                        # home dir blocks qemu from reading
                                        # the overlay disk's backing file)

   [base_image]
   url = "https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img"

   [defaults]
   vcpus = 6
   memory_mib = 12288
   disk_gb = 100

   [ssh]
   pubkey_path = "~/.ssh/id_ed25519.pub"

   [jj]
   name = "Jim Posen"
   email = "jim.posen@gmail.com"

   [env]                                # creds/vars injected into every box (see below)
   GITHUB_TOKEN = "github_pat_..."

   [aws]                                # shared AWS account creds — needed for
   region = "us-east-1"                 #   --provider ec2 and --provider aws
   profile = "dev"                      # optional AWS named profile

   [ec2]                                # optional — EC2-specific defaults
   default_security_groups = ["sg-..."] # optional
   default_subnets = ["subnet-a", "subnet-b"]  # optional; tried in order (capacity fallback)
   spot = true                          # optional default market (workspaces can override)
   ```

   `[aws]` holds the shared AWS account credentials and is what enables **both**
   AWS-backed providers (**ec2** and **aws**/Lightsail); `[ec2]` holds the
   EC2-specific knobs (both sections optional). Lightsail has no global section
   at all — its knobs are all per-workspace, under `[lightsail]`. Omit them
   entirely on a libvirt-only host — `devbox list` then never reaches for
   AWS credentials. Single region/profile only for now (multi-region/profile
   deferred). `boto3` is a declared dependency, so it's always installed, but
   only touched when you actually use AWS.

   **Expired credentials.** Any command that talks to AWS resolves the
   profile's credentials before its first API call, so a lapsed session is
   caught up front rather than as a boto stack trace mid-command:

   ```
   AWS credentials for profile 'dev' are missing or expired (The SSO session
   associated with this profile has expired or is otherwise invalid).
   Run `aws sso login --profile dev` now? [Y/n]
   ```

   Answering yes runs the login and the command carries on. The offer only
   appears for SSO profiles (nothing devbox could run would refresh static
   keys) on a TTY — a script, or a non-SSO profile, gets the same one-line
   message telling it what to fix. A stale AWS session never blocks a libvirt
   `create`: the cross-provider name-collision check degrades to a warning.

   `[jj]` is optional — if unset, `devbox create` falls back to the host's
   own `jj config get user.name`/`user.email` (which you almost certainly
   already have set, if you use jj). It's only truly required if that
   fallback also fails (no `jj` on the host, or no identity configured
   there either), in which case `devbox create` dies with a clear message.
   Whichever source resolves, the identity is what the built-in `jj`
   module writes into the agent's `~/.config/jj/config.toml` on the box.
   home-manager owns that file, so it is a read-only symlink into the nix
   store and `jj config set --user` on the box fails; jj also reads
   `~/.config/jj/conf.d/*.toml`, which nothing manages and which takes
   precedence, so that is where a by-hand override goes.

   `[env]` is a map of environment variables — scoped credentials, API
   keys — injected into every box (see [Credentials](#credentials-env) below).
   A workspace's `[env]` overrides it per-key. **`GITHUB_TOKEN` is the way to give
   the sandbox GitHub auth**: `gh`/`git`/`jj git` pick it up so private clones
   and pushes work. Use a
   [fine-grained personal access token](https://github.com/settings/personal-access-tokens)
   scoped to just the repos the sandbox needs. Optional — a box with no
   `GITHUB_TOKEN` just has no GitHub HTTPS auth (fine for public repos).

   There's no `sha256` field: `devbox create` fetches `SHA256SUMS` +
   `SHA256SUMS.gpg` from alongside the image URL, verifies the signature
   against Canonical's cloud-image signing key (`gpgv` +
   `ubuntu-cloudimage-keyring`, from Requirements above), and uses the
   checksum it lists. This avoids pinning a hash by hand — `.../release/...`
   URLs are mutable (Canonical overwrites the file in place on respins), so
   a hand-pinned checksum inevitably goes stale and breaks `create` until
   someone updates it.
2. **SSH `Include`** — `devbox host` (see "Exchanging commits" below) appends `Include
   ~/.config/devbox/ssh/*.conf` to `~/.ssh/config` the first time it runs, so
   there's nothing to do here manually; it's idempotent.

## Per-workspace setup

Write the marker — `.devbox/config.toml` inside the repo, or `devbox.toml` in a
directory that holds repos (see "Where the marker goes" above). Either way it is
**not committed**: for the `.devbox/` shape, add `.devbox/` to that repo's
`.git/info/exclude` (local-only, invisible to other contributors — there's
nothing workspace-shareable in here, it's purely this machine's VM config); the
`devbox.toml` shape sits outside every repo and needs no excluding at all.

```toml
# workspace = "my-workspace"  # this workspace's name
#                           # (default: the directory's basename)
# box = "dev"               # the box to attach to, by canonical instance name
#                           # (default: the workspace's own name, i.e. a box of
#                           # its own). Create it with `devbox box create`.
# agent_user = "my-workspace" # the UNPRIVILEGED account agents run as (no sudo).
#                           # Default: the workspace name. Must be a legal Linux
#                           # username and not a system or cloud-image account —
#                           # validated up front. It must also differ from the
#                           # box's privileged account, which is only knowable
#                           # at attach time, so that is checked there.
# mods = ["rust"]           # provisioning mods appended after the base playbook
#                           # (see "Provisioning: mods" below)

# One [[repos]] block per checkout; omit them all for no repo bootstrap. Every
# repo lands in the same account, so they share its git/jj credentials.
[[repos]]
name = "my-repo"            # addresses the repo, and supplies the two defaults
                            #   below. Omit it ONLY under a `.devbox/` marker,
                            #   where it means "the workspace root is the repo".
mode = "clone"              # "clone" | "push"
url = "https://github.com/<org>/my-repo"   # required iff mode == "clone"
# host = "my-repo"          # host path, relative to the workspace root
#                           #   (default: `name`; "." for a nameless entry)
# dir = "my-repo"           # where it lands in the agent's home, ~/<dir>
#                           #   (default: `name`)

# [env]                     # creds/vars injected into the box's shell env;
# GITHUB_TOKEN = "github_pat_..."   #   overrides the global [env] per-key. A
#                           #   repo-scoped PAT belongs here (the marker is
#                           #   git-excluded).

# Host->box file sync (rsync); see "Host->box file sync" below.
# [[sync]]
# src = "$HOME/.claude/skills"   # $HOME/~/absolute: resolved per side
# include = ["jujutsu"]          # only these children of src (each mirrored)
# [[sync]]
# src = "notes"                  # bare-relative: workspace root -> ~/notes.
#                                #   `devbox.toml` markers only.
# [[repos.sync]]                 # nested under a [[repos]] entry: anchored at
# src = ".claude/skills"         #   that checkout on both sides.

```

**Hardware is not workspace config.** `provider`, `user`, `vcpus`, `memory_mib`,
`disk_gb`, `[ec2]` and `[lightsail]` describe a *box*, so they are flags to
`devbox box create` and devbox errors if it finds any of them in a workspace's
config — silently ignoring `vcpus = 16` would boot the wrong-sized machine.

```bash
devbox box create dev --vcpus 8 --memory 32768 --disk 200
devbox box create dev --provider ec2 --instance-type g5.xlarge --arch x86_64 [--ami ami-0123... --spot]
devbox box create dev --provider aws --bundle xlarge_3_0 [--blueprint ubuntu_24_04 --zone us-east-1b]
```

Anything not given falls back to the global `[defaults]`. `--disk` is ignored on
Lightsail, whose bundle fixes CPU/RAM/disk together.

Box names encode the provider so every command can route to the right backend
from the name alone — no API call or credential load just to find a box's owner.
The scheme:

- **Canonical name** = `<provider>-<name>` for cloud providers, **bare** for
  libvirt (local is the common case). So `box create binius-arm --provider ec2`
  composes **`ec2-binius-arm`** — that's the identity `devbox box list` shows,
  what a workspace's `box =` key holds, and what every later command references.
- Commands derive the provider by parsing the name: the first `-`-delimited
  segment routes to that provider if it's a known keyword (`ec2`, `aws`, …),
  else libvirt.
- The `<provider>-` prefix is **added automatically** — pass the bare name to
  `box create` (`binius-arm`, not `ec2-binius-arm`). devbox rejects a name that
  already starts with a known provider prefix, so it can't misparse. The same
  goes for a workspace name that would double as a bare libvirt box name.
- Provider-side, the prefix is redundant, so the **AWS-side name drops it**:
  `ec2-binius-arm` shows as `devbox-binius-arm` in the EC2 console, and
  `aws-binius-arm` is created as the Lightsail instance `binius-arm`. devbox's
  own `devbox:name` addressing tag keeps the canonical prefixed name on both.

Box names are globally unique across providers (the prefix makes that fall out
for free). Every workspace config key is optional; an empty `config.toml` is a
valid marker. `name` is no longer a field — devbox errors if it's present, to
flag the migration.

The same re-runnable provisioning (jj, dotfiles, Claude Code, `[[repos]]`
bootstrap) runs over SSH after boot on every provider, so a sandbox is identical
whether it's a local libvirt VM, an EC2 instance or a Lightsail box.

The **aws** provider is AWS **Lightsail**: the same init script as user-data and
the same provisioning, but flat-rate *bundles* instead of an instance type plus an
EBS volume. Trade-offs worth knowing:

- **Bundles fix the machine.** A bundle sets CPU, RAM, and disk together, so
  `--disk` is ignored on this provider. The default, `xlarge_3_0`, is 16 GB RAM
  / 4 vCPU / 320 GB SSD (~$84/mo). Pass `--bundle` to pin any other tier. There
  is no architecture knob: Lightsail's Linux bundles are **x86-only** (no
  ARM/Graviton tier), unlike EC2.
- **The OS trails a little.** `--blueprint` defaults to `ubuntu_24_04`, the newest
  Ubuntu LTS blueprint Lightsail offers — an older userland than the Ubuntu
  26.04 cloud image the libvirt and ec2 providers boot. The mods provision the
  same way on it; pin `blueprint` if you want something else.
- **Dynamic public IPv4.** No static IP is allocated (so none is left behind to
  pay for or release); `devbox ip`/`ssh` re-resolve the current address on every
  command, which is what makes a post-`stop`/`up` address change a non-event.
- **Port 22 is open by default** on a Lightsail instance, so there are no
  security groups to configure — hence no global `[lightsail]` section.
- **No spot market:** `--spot`/`--on-demand` are EC2-only and are ignored here.

`devbox box create binius-box --provider aws` needs nothing but `[aws]` creds and
no other flag at all; a workspace then selects it with `box = "aws-binius-box"`.

> **On IMDS and instance roles.** devbox attaches **no** instance profile on
> either AWS provider, so there are no role credentials to steal at
> `169.254.169.254` today. If you ever add one, note that the unprivileged agent
> reaches the metadata endpoint exactly as easily as root does — an instance
> role would hand it AWS credentials outright, straight past the account split.
> Block IMDS for the agent user (an iptables owner-match rule on that address) in
> the same change that attaches the profile.

`[[repos]]` drives how the sandbox gets your code, via the built-in
`repo-bootstrap` mod (see below) — no workspace needs to author this by hand.
Each entry's `mode` says how that one checkout arrives:

- **`mode = "clone"`** — the repo already has a git remote. The sandbox runs
  `jj git clone <url> ~/<dir>` while provisioning.
- **`mode = "push"`** — nothing pushed anywhere yet. The sandbox creates an empty
  colocated jj+git repo at `~/<dir>`, ready to receive a push from the host — see
  "Exchanging commits" below. `url` is not used (and not allowed) in this mode.
- **List no repos at all** — no repo bootstrap, e.g. a scratch/experiment VM.
  (There is no `mode = "none"`: an entry that does nothing has no reason to be
  written down.)

`repo-bootstrap` is guarded per directory, so a reconverge creates only the
checkouts that are missing and leaves the rest alone — which is what makes adding
a repo to a live workspace a `devbox sync --mods=repo-bootstrap`.

A workspace adds everything it needs as a **mod** (see below). There is no
per-workspace first-boot hook: devbox's first-boot layer is one fixed init script
(see [First boot](#first-boot-the-init-script)), and the older
`.devbox/cloud-config.yaml` fragment is **gone** — port anything you had there
to a workspace mod, which reconverges in place instead of forcing a rebuild.
Provisioning context (jj identity, the account names, the GitHub token) flows to
mods via the environment, not `%TOKEN%`s. The checkouts are the exception: there
can be any number of them, so they ride along as a `repos` file in the payload,
which `repo-bootstrap` reads through `DEVBOX_PAYLOAD_DIR`.

## Provisioning: mods

Provisioning is a list of small, idempotent, composable bash scripts — **mods**
(inspired by Ansible roles, without Ansible). A mod installs/configures one
thing (jj, dotfiles, Claude Code, a workspace's rust toolchain, …). There are **two
playbooks**, because a box and an attachment need different things:

```
box playbook (fixed, built-in):     dotfiles-packages → tree-sitter-cli →
                                    dotfiles → vim → github-auth → nix
                                    # runs once per box, as root / the admin

workspace playbook (built-in base):   home-manager → jj → dotfiles → vim →
                                    neovim → claude-code → cargo-binstall →
                                    repo-bootstrap
workspace mods (config.toml):         + rust + wasm-tools + …
                                    # runs once per attachment, in its account
```

Ordering encodes the constraints (jj before repo-bootstrap, which clones with
it; `dotfiles-packages`, the apt half, before `dotfiles`, the per-user half).
`github-auth`, the other half of a private `jj git clone`, sits in the box
playbook, which runs first. The 95% case is "just add rust":
`mods = ["rust"]`.

**A box takes no config, so its playbook is fixed** — `dotfiles-packages` (apt,
as root), `github-auth` (one `/etc/gitconfig` line) and `nix` (the multi-user
nix store and its daemon, which every attached workspace shares) are the
genuinely box-level mods in the base; everything else installs into somebody's
home. A workspace that needs more *system* state ships a
`runas = ["root"]` mod of its own, which runs on the shared box when it attaches;
devbox prints a warning naming it, since that writes state every other workspace
sees.

**`home-manager` is the declarative half of an attachment's home.** It runs
first in the workspace playbook and switches the agent account to the
home-manager flake that rides in the payload beside `mods/` (`nix/flake.nix`,
`nix/options.nix`, `nix/modules/<mod>.nix`, plus a `nix/workspace.nix` devbox
renders per attachment). A mod is either a bash script or a module in that
flake, never both, and a converted mod keys on its own
`devbox.mods.<name>.enable` flag. It is **agent-only**: the box's privileged
account keeps its apt zsh/vim, so `devbox box ssh` works whatever state a
generation is in. Workspace-local `.nix` modules are not supported — a
workspace that needs something of its own ships a bash mod, exactly as before.
Nothing from `[env]` may appear in a nix expression: `/nix/store` is
world-readable and shared by every attachment.

**`dotfiles` and `vim` are in both playbooks on purpose** — each declares
`runas = ["admin", "agent"]`, and each scope runs only its own half. The box runs the
admin half once (so `devbox box ssh`, the debugging shell, is usable) and each
attachment runs the agent half in its own account, without redoing the admin's.

**Resolution** — each mod name resolves to a `<name>.sh` script by precedence
**workspace > global > built-in**, so a workspace can shadow a built-in by name:

```
mods/*.sh                       # built-in, at the repo root
~/.config/devbox/mods/*.sh      # global user (shared across workspaces)
<workspace>/mods/*.sh           # workspace-local, beside a devbox.toml marker
<repo>/.devbox/mods/*.sh        #   ... or inside a .devbox/ one
```

A workspace's own mods live beside its config file, whichever spelling it uses.

When **no** directory has a `<name>.sh`, the name resolves instead to the
home-manager module of that name (`devbox/nix/modules/<name>.nix`), and the
generation provides the mod rather than the driver running a script. That is all
converting a mod takes — write the module, delete the script — and it is why a
script always wins: a workspace that ships its own `jj.sh` gets its own jj,
converted or not, and never both.

**Static files, env for context.** Mods are static (lintable, shellcheck-able,
diffable, and directly runnable — the metadata below lives in a fenced block of
comments, so the shebang stays on line 1) and receive context via the **environment**
(`DEVBOX_AGENT_USER`, `DEVBOX_ADMIN_USER`, `DEVBOX_JJ_NAME`, `DEVBOX_WORKSPACE`,
…), not `%TOKEN%` substitution. The one thing too long for an environment
variable is the list of checkouts, which arrives as a `repos` file in the payload
(`$DEVBOX_PAYLOAD_DIR/repos`, one `<name>\t<mode>\t<dir>\t<url>` record each).
Each mod also sources the `[env]` creds file of
the account that owns them (see [Credentials](#credentials-env)) — its target
user's, or the privileged user's for a `runas = ["root"]` mod — so a mod that needs
a secret (`repo-bootstrap` cloning a private repo with `GITHUB_TOKEN`,
`cloudflared-tunnel` running a tunnel with `CLOUDFLARE_TOKEN`) has it, and it
never lands in a script on disk, in a command line, or in a log.

**A mod body runs without sudo.** The driver runs as the privileged user and
drops to the agent for each mod, because configuring a user's home directory is
what a mod almost always does. So `sudo` inside a mod is a bug — use the two
metadata keys below instead. This is also why the payload carries no secrets: it has to
be readable by the account the driver drops into.

**Metadata lives in a `# /// mod` block.** A mod declares `requires`, `runas`
and `packages` in one fenced block of comments holding TOML, directly after the
shebang:

```bash
#!/usr/bin/env bash
#
# /// mod
# requires = ["jj", "github-auth"]
# runas = ["admin", "agent"]
# packages = ["jq", "unzip"]
# ///
```

The shape is [PEP 723](https://peps.python.org/pep-0723/)'s — Python's inline
script metadata, what `uv run` reads — and the same idea as LSB init scripts'
`### BEGIN INIT INFO` or mise's `#MISE`. Being **fenced** means the boundary is
explicit, so prose elsewhere in the file that happens to mention a key is just
prose. Being **comment-prefixed** means a mod stays a plain script: shebang on
line 1, `shellcheck` clean, `./mods/rust.sh` still runs. TOML rather than YAML
because it's `tomllib` in the standard library (no dependency) and it is already
the language of devbox's own config files.

Every key is optional, each is an array of strings, and devbox hard-errors on an
unknown key, a malformed value, a duplicate block, or a mislabeled fence — at
playbook-resolution time, before any boot or SSH. Parsing is entirely host-side;
the driver receives a resolved manifest and never reads a mod file.

**Who a mod runs as (`runas`).** Default `["agent"]`. Also `admin` (the
privileged account) and `root`. A mod may name more than one, and then runs once
per target that is in scope. That key is also what decides scope, so there is no
second, drifting `scope` key to keep in sync:

| the mod declares | in the **box** playbook it runs | in a **workspace** playbook |
|---|---|---|
| an `agent` target (e.g. `admin agent`) | everything *except* the agent | *only* the agent |
| no agent target (e.g. `root`) | all of its targets | all of its targets |

The filter is applied entirely host-side — the manifest the driver receives has
already been narrowed — so the driver itself never reasons about scopes.
Validated before any boot/SSH; an unknown target is a hard error.

**System packages (`packages`).** `packages = ["jq", "unzip"]` — apt-installed by
the driver, as the privileged user, before the body runs. This is the sanctioned
route now that a mod body has no sudo. The index is refreshed once per run. Note
that apt is box-wide: a package one workspace asks for is installed for everybody.

**Delivered over SSH** (one path). devbox tars the resolved driver + mods +
context + manifest + `repos`, pipes it to the box over the `devbox-<box>-admin` SSH alias
(the driver needs sudo), extracts it into a per-run temp dir, and runs the
driver. This is the **only** delivery path — the same for a fresh box and a
reconverge:

- **`devbox box create`** — after boot, runs the box playbook.
- **`devbox create`** — attaches: creates this workspace's account, injects the
  `[env]` creds, runs the workspace playbook, then pushes `[[sync]]` files. It
  blocks through all of it, so when it returns the workspace is fully provisioned.
- **`devbox sync --mods[=a,b,c]`** / **`devbox box sync --mods`** — reconverge a
  live box; no list runs the whole playbook, a list runs just those (fast
  iteration while writing a mod).

The init script carries **no** provisioning payload — it only does base setup.
(Trade-off: the host must stay attached during `create`; if it disconnects
mid-provision, re-run `devbox sync --mods`.)

The driver runs each mod in playbook order, stops at the first failure (naming
which mod and which account), and tees each mod's output to
`~/provision/<agent_user>/<name>.log` — `<name>.<user>.log` for a mod that runs
for more than one account — plus the combined `~/provision-<agent_user>.log` that
`devbox status` tails. Both are **per attachment**, so two workspaces reconverging
don't overwrite each other's; a box run uses `~/provision/box/` and
`~/provision.log`. All of these live in the **privileged** user's home, since
that is where the driver runs.

Before any mod, on every run, the driver ensures the accounts it is about to use:
it wires the privileged user's `~/.bash_env`, then — for a workspace run — creates
that workspace's account if absent (no supplementary groups, `0750` home, locked
password, your SSH key authorized), enables lingering for it so its systemd user
services outlive an SSH session, records who owns it in
`/etc/devbox/workspaces/<agent_user>`, and **refuses to continue** if the account
belongs to another workspace, if it exists with no record at all (pass `--adopt`),
or if the agent has somehow acquired sudo or a root-equivalent group. It is a
driver preamble rather than a mod on purpose: it must always run, must not be
shadowable by a workspace's own mods directory, and must not be skippable via
`devbox sync --mods=dotfiles`.

**Idempotency contract:** every mod either overwrites unconditionally or guards
on "already done", so `create` and every reconverge are safe. Each built-in
mod's header prose documents which pattern it uses.

**Declaring dependencies (`requires`):** a mod that depends on another running
first declares it as `requires = ["jj", "github-auth"]`. devbox parses this
statically and, at playbook-resolution time (before any boot/SSH), hard-errors if
a required mod is missing from either playbook or listed *after* the mod that
needs it. The box playbook is indexed first, so a workspace mod may depend on a box
mod — the machine exists before any attachment does. It is **not** a resolver —
it never reorders or auto-inserts; it just validates order (so a future reorder
can't silently break things). Built-in examples: `repo-bootstrap`
`requires = ["jj", "github-auth"]`, `vim` `requires = ["dotfiles"]`.

### Example: a rust workspace

The marker gains one line, and the rust setup is a workspace-local mod — nothing
else to configure:

```toml
# .devbox/config.toml  (or devbox.toml)
mods = ["rust"]
```

```bash
# .devbox/mods/rust.sh
#!/usr/bin/env bash
#
# /// mod
# packages = ["clang", "libssl-dev"]
# ///
#
# No `runas` — it defaults to the agent, whose $HOME this installs into.
# No `sudo` either: the body has none. Packages go in the block above, where
# the driver installs them as the privileged user before this runs.
set -euo pipefail
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
. "$HOME/.cargo/env"
# ... cargo tools, claude plugin install rust-analyzer-lsp, etc.
```

`devbox sync --mods=rust` re-applies just that mod on a live box.

### First boot: the init script

devbox ships one static bash script, `devbox/init-script.sh`, and hands it to
**every** provider as user-data — libvirt (NoCloud seed), ec2 (`UserData`) and
aws/Lightsail (`userData`). cloud-init executes a `#!` user-data script on
libvirt and EC2 (its scripts-user stage); Lightsail executes user-data as a
shell script natively — it does **not** understand `#cloud-config`, which is
why devbox has no cloud-config layer at all.

`devbox box create` renders it by literal `%TOKEN%` substitution (not `{{...}}`,
which collides with cloud-init's jinja): `%DEVBOX_USER%`, `%DEVBOX_SSH_PUBKEY%`
and `%DEVBOX_HOSTNAME%` (the box's canonical name). What it does, as root,
under `set -euo pipefail`:

1. Creates the **privileged** user — `sudo` group, a `/etc/sudoers.d` NOPASSWD
   drop-in, locked password, `0750` home, and `~/.ssh/authorized_keys` holding
   your public key. This is **first**, before the package install, so SSH access
   is never gated on (or lost to) a slow or failing apt step.
2. Sets the hostname to the instance name. It also drops a `preserve_hostname`
   cloud-init snippet, so cloud-init doesn't revert the name to the
   datasource's on the next reboot.
3. Installs `/etc/devbox/bash_env` and points `/etc/environment` at it with
   `BASH_ENV=/etc/devbox/bash_env`, so non-interactive bash (e.g. Claude Code's
   Bash tool) picks up what the mods install. It is a **shared stub** that
   sources `$HOME/.bash_env`, because `/etc/environment` is one system-wide
   table and a box has several accounts — a per-user path there would make one
   user source another's file. (`BASH_ENV=$HOME/…` is not an option: pam_env
   reads `/etc/environment` as plain `KEY=VALUE`, so the expansion has to happen
   in bash.)
4. Adds the official GitHub CLI apt repo, then installs the baseline the mods
   lean on: `zsh vim python3 curl git git-lfs ca-certificates gh rsync
   build-essential pkg-config`. (The repo is needed because `gh` isn't in every
   Ubuntu release's archive — notably 24.04, the newest LTS blueprint Lightsail
   offers — and under `set -e` a missing package would abort first boot.) No
   distro upgrade.
5. Switches the login shell to `zsh` now that it exists.

It deliberately creates **no** workspace accounts. User-data runs once, at first
boot, so an account born here could never appear on an existing box: attaching a
second workspace — or changing an `agent_user` — would need a rebuild. The
provisioning driver creates them instead, where they reconverge. This is also why
first boot takes a *box*, not a workspace: it sets up the machine, and knows
nothing about who will later live on it.

Every step is idempotent, and the whole thing carries no provisioning payload —
mods run over SSH afterwards. A workspace cannot extend this layer (the
`.devbox/cloud-config.yaml` fragment is gone); workspace-specific setup is a mod,
which has the advantage of reconverging on a live box.

## Credentials (`[env]`)

`[env]` is a map of environment variables — scoped credentials, API keys —
hardcoded in `config.toml` and injected into a box. Global `[env]`
(`~/.config/devbox`) applies to every box; workspace `[env]` (`.devbox`, which is
git-excluded) adds workspace-specific keys and **overrides the global map
per-key**.

```toml
[env]
GITHUB_TOKEN = "github_pat_..."                          # the agent's
SOME_API_KEY = "..."
REGISTRY_TOKEN = { value = "...", owner = "admin" }      # the privileged user's
```

**Each entry belongs to exactly one account.** A bare string goes to the agent —
the account that does the work, and the right place for anything an agent or a
mod configuring its home needs. The table form with `owner = "admin"` **moves**
the entry to the privileged user instead: it never enters the agent's
environment, and a privileged mod (`runas = ["admin"]`, or `["root"]`, which
reads the privileged user's file) gets it. That is for creds an agent has no use
for — what a `runas = ["root"]` mod needs to reach a private package repo, say.
`owner = "agent"` is the default and spells out the common case.

**In the guest**, devbox writes one managed 0600 file per account, in that
account's home — `~/.config/devbox/env` (`export KEY=val` lines) — and sources it
from **both** that user's `~/.zshenv` (interactive zsh) **and** its `~/.bash_env`
(the `BASH_ENV` target). The second one matters: Claude Code's Bash tool runs
*non-interactive bash*, which doesn't read `.zshenv` — so creds only in `.zshenv`
would be invisible to the agent and anything it runs.

Homes are 0750 with a per-user group and every file 0600, so no agent can read
the privileged user's file — the whole point of `owner` — nor another workspace's.
The privileged side can read them all; it is the trusted side of the box.

Note the asymmetry a shared box introduces: the *global* `[env]` applies to every
box, so a token there now reaches every workspace's account on a machine it shares
with unrelated workspaces. Prefer a workspace's own `[env]` for anything scoped.

**GitHub auth** is just `GITHUB_TOKEN` in `[env]`: `gh` reads it natively (no
`gh auth login`, so nothing lands in `~/.config/gh/hosts.yml`), and the
`github-auth` mod points git's credential helper at `gh auth git-credential` so
`git`/`jj git` HTTPS use it — private clones and pushes work. The clone URL stays
plain HTTPS; gh is the helper behind it, not the transport.

That helper line lives in **`/etc/gitconfig`**, written once per box as root. It
holds no secret — it names a helper, and the helper answers with whatever
`GITHUB_TOKEN` the *calling* account has in its own `~/.config/devbox/env`, so
one system-wide line is right for every account on the box, the privileged one
included. It is also out of reach of `dotfiles`, which copies its own gitconfig
over `~/.gitconfig`. With no token the helper prints nothing and exits 1, git
falls through, and public clones are unaffected.

The box playbook runs at `devbox box create` and `devbox box sync --mods`, not
at `devbox create` — so an existing box picks this up on its next `devbox box
sync --mods`.

**Delivery & idempotency:** `devbox sync --envvars` ships the map over SSH (piped,
never echoed to a log), rewriting both managed files in full each run — so
removing a key from config, or handing it to the other `owner`, removes it from
the account that had it. Auto-run first on `devbox create`.

**Security:** plaintext in `config.toml` (host) and the 0600 files (guest) — an
accepted trade-off for scoped creds on a personal VM. The real change from a
credential-helper store is that an agent-owned value lives in the **process
environment** of everything the agent runs (the point), so it's visible via
`/proc/<pid>/environ` to that account; `owner = "admin"` is how a cred stays out
of that. Keep them out of provision logs (devbox delivers them piped, not
substituted).

## Host→box file sync

`devbox sync --files` pushes **host-local, non-VCS-tracked files** onto the box —
primarily agent **skills** (which change often) from wherever they live on the
host (`~/.claude/skills`, a workspace's git-excluded skills) to wherever they go
on the box. This is a distinct problem from provisioning: a **mod runs on the
box and can't read host-local files**, so host-only files fundamentally need a
host-side push. (Skills that live in a tracked repo arrive via `[[repos]]`; skills
published on GitHub belong in a mod that clones them box-side — `--files` is only
for host-local files.)

Configure it with `{ src, dst?, include? }` entries. Every rule carries its own
**anchor** — what its two ends are relative to — and all three anchors put the
box end under the agent's home, since that is where a workspace lives:

```toml
# Repo-anchored: nested under a [[repos]] entry, so it never has to name one.
# Host = that checkout; box = ~/<its dir>.
[[repos]]
name = "frontend"
mode = "push"
[[repos.sync]]
src = ".claude/skills"                 # -> ~/frontend/.claude/skills on the box

# $HOME/~/absolute-anchored, resolved per side (host home reading src, guest
# home writing dst).
[[sync]]
src = "$HOME/.claude/skills"
include = ["jujutsu"]                   # only this child of src, not every skill

# Workspace-anchored: host = the workspace root, box = the agent's home.
# `devbox.toml` markers only — see below.
[[sync]]
src = "shared-notes"                   # -> ~/shared-notes on the box
```

**Why workspace-anchored paths need a `devbox.toml` marker.** That shape lines up
only when the workspace root *holds* the repos: then the root and the agent's
home carry the same child names, and `notes` means the same place on both sides.
Under a `.devbox/` marker the root is itself a repo while the box-side home is
not, so the two ends would quietly mean different things — devbox refuses it and
points at `[[repos.sync]]`.

- **`dst` defaults to `src`**, and `$HOME`/`~` resolve **per side** — so the
  common "same relative location both ends" case needs only `src`. Only
  `$HOME`/`~` are supported (not arbitrary env vars — guest-side expansion would
  mean running a shell on the box); both sides resolve offline. An anchored `src`
  picks the `$HOME`/absolute anchor; a bare-relative one picks the workspace (at
  the top level) or the repo (under a `[[repos]]` entry).
- **`include`** narrows to named children of `src` (names only for now). Its key
  subtlety is that it **defines the mirror unit**: an entry with `include`
  expands to one rsync per child (`src/jujutsu → dst/jujutsu`), each mirrored
  (`--delete`) within its *own* subtree — so `include = ["jujutsu"]` never
  `--delete`s box-installed skills outside the list. Without `include`, the whole
  `src` is one mirror of `src → dst`.

**Mechanism:** `rsync` over this workspace's own SSH alias, idempotent and
incremental (cheap to run often). **Host wins** — a mirror deletes host-removed
files within a synced subtree, so an agent's box-side edits to a synced skill
are clobbered on the next sync (acceptable for host-authored skills).

**When it runs:** on demand via `devbox sync --files`, and automatically as the
last step of `devbox create` (after the mods run, since the sync dsts depend on
the checkouts being in place). Files and mods are separate flags of `devbox
sync` precisely because skills change on a faster cadence than provisioning —
`devbox sync --files` alone is cheap. (`rsync` must be installed on the host; it
ships in the box's base packages.)

## Exchanging commits with the sandbox (jj)

The sandbox is a plain SSH-reachable git remote for the host, in both
directions: the host **fetches** the agent's work to view/test it, and, for
repos with no git server yet (`mode = "push"`), the host **pushes**
to seed the sandbox with an initial commit. This is **identical whether the box
is libvirt, EC2, or Lightsail** — the address is resolved through the
provider (libvirt DHCP lease, or the cloud instance's public IP); everything
above that is the same
jj-remote model. The remote points at this workspace's own account, so two
workspaces on one box fetch from two different homes over two different aliases.

A workspace holding several repos uses **one alias for all of them** — the alias
identifies the account, and each repo is a different path inside it. So
`devbox jj fetch` needs to know which checkout you mean: it takes the one you
name (`devbox jj fetch backend`), else the one your working directory is inside,
else the workspace's only one.

Both directions go through this workspace's SSH alias, which `devbox jj fetch`
and `devbox host` set up/refresh:

- A per-box known-hosts file, `~/.config/devbox/ssh/known_hosts.<box>`,
  **truncated when that box is created** (a freshly created box has a brand-new
  SSH host key, so stale entries can only cause false mismatches). Per box, not
  shared: creating a *different* box must not invalidate this one's pinned key
  for every workspace attached to it.
- A `Host devbox-<box>-<workspace>` block — collapsing to `devbox-<box>` when the
  workspace owns the box — alone in `~/.config/devbox/ssh/<box>.<workspace>.conf`,
  with the box's current IP (from the provider), the workspace's account, and
  `StrictHostKeyChecking accept-new` scoped to that box's known-hosts file. It is
  regenerated on every call since the address can change across `stop`/`up`
  cycles (a libvirt DHCP lease, or a new public IP after stop/start — Lightsail
  boxes use a dynamic IPv4, so this is expected there rather than exceptional).
  One stanza per file is what keeps attachments independent: a single shared file
  per box would have each workspace's refresh overwrite the last one's account.
- A one-time, idempotent `Include ~/.config/devbox/ssh/*.conf` line in
  `~/.ssh/config`.

### Fetch: pulling sandbox commits to the host (`mode = "clone"`)

The sandbox already cloned the repo while provisioning (via `[[repos]]`), so
there's no manual clone step. The agent just bookmarks work it wants visible
on the host — jj only exports *bookmarked* commits to git refs, which is what
the host's fetch reads:

```bash
# Sandbox side (agent), per unit of work that should become visible to the host:
jj bookmark create wip -r @

# Host side (from your local jj checkout): one command, any provider.
devbox jj fetch                      # refreshes the alias, wires the jj remote, fetches
jj new wip@devbox-my-workspace         # check out the fetched bookmark
```

`devbox jj fetch` picks the checkout (see above), refreshes the SSH alias (address
via the provider), ensures a jj git remote named after the alias points at that
colocated repo (`ssh://<alias>/home/<agent_user>/<dir>`), and runs `jj git
fetch` in the matching host repo. It's equivalent to the manual flow:

```bash
devbox host                                             # prints alias, e.g. devbox-my-workspace
jj git remote add devbox-my-workspace ssh://devbox-my-workspace/home/<agent_user>/<dir>
jj git fetch --remote devbox-my-workspace
```

### Push: seeding a brand-new sandbox (`mode = "push"`)

For a repo with no git remote yet, the sandbox's common provisioning
creates an *empty* colocated jj+git repo at `~/<dir>` while provisioning
(`git init --initial-branch=main`, `jj git init --colocate`, then
`git config receive.denyCurrentBranch updateInstead` — the setting that lets
a push update a non-bare repo's checked-out working tree). The host seeds it:

```bash
devbox host                                             # prints alias, e.g. devbox-my-workspace-dev
jj git remote add sandbox ssh://devbox-my-workspace-dev/home/<agent_user>/<dir>
jj bookmark set main -r @ --allow-backwards   # the bookmark name MUST be "main" -
                                                # matches the sandbox repo's initial-branch
jj git push --remote sandbox --allow-new -b main
```

Two safety properties worth knowing, both verified directly against real
git behavior (not assumed): `updateInstead` only updates the sandbox's
working tree when the pushed branch matches the one currently checked out
— hence the bookmark name being pinned to `main` — and it **refuses the
push outright if the sandbox-side working tree is dirty**, rather than
silently clobbering uncommitted agent work. If a push is rejected for that
reason, don't force it; check what's uncommitted in the sandbox first.

Once seeded, the same `sandbox` remote's fetch flow (above) works normally
for pulling the agent's subsequent work back out.

## Development

```bash
uv sync              # create .venv with dev dependencies (pytest)
uv run pytest        # run the test suite
```

The code is a `devbox/` package: `config` (global/workspace config, marker
resolution, `[[repos]]`, `BoxSpec`),
`provision` (modular mods — resolution, scope, env context, payload/driver
assembly), `initscript` (first-boot user-data script render), `sync` (host→box
rsync file sync), `cli` (subcommands + argparse), and a `providers/` package — a
`Provider` seam (`providers/base.py`) with `libvirt`, `ec2`, and `lightsail` (the
`aws` provider) implementations. Data files
(`init-script.sh`, `provision-driver.sh`) live inside the package; the built-in
`mods/*.sh` live at the repo root.
