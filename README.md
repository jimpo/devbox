# devbox

My opinionated tool for launching and configuring development workspaces in isolated virtual machines. The primary use case is sandboxed agentic development, and
cross-platform development and benchmarking on cloud VMs.

`devbox` provides a CLI for managing these workspaces and VM instances and a declarative configuration system. Workspace configuration relies heavily on [Nix Home Manager](https://nix-community.github.io/home-manager/). This tool ships with an agent skill (`skills/devbox/`) to assist with configuration and usage.

`devbox` currently supports three VM providers:

- **libvirt** (local VMs),
- **ec2** (AWS EC2), and
- **aws** (AWS Lightsail — flat-rate bundles, simpler than EC2).

## Requirements

- **libvirt** provider: Debian/Ubuntu with libvirt + qemu, run as a member of
  the `libvirt` group:
  ```
  sudo apt install virtinst libvirt-daemon-system qemu-system-x86 ubuntu-cloudimage-keyring
  ```
  The keyring is what devbox verifies the base image's `SHA256SUMS` against.
- **ec2** / **aws** providers (optional): AWS credentials (e.g. a named profile)
  with EC2 / Lightsail permissions, and an `[aws]` section in the global config.
- [`uv`](https://docs.astral.sh/uv/).
- `jj` and `rsync` on the host, for `devbox jj fetch` and `devbox sync --files`.

## Install

```bash
uv tool install --editable .
```

Installs `devbox` into `~/.local/bin`. Being editable, pulling new commits needs
no reinstall; add `--reinstall` after a dependency change.

Copy or link `skills/devbox/` into `~/.agents/skills`.

## Quickstart

Mark a directory as a workspace and say how the code gets onto the box:

```toml
# ~/src/my-workspace/.devbox/config.toml
[[repos]]
mode = "push"    # the box gets an empty repo ready to receive `jj git push`
```

Then, from anywhere inside it:

```bash
devbox create      # boot a box named after the workspace, then attach: create its
                   #   account, inject creds, run the mods, push [[sync]] files
devbox ssh         # log in as this workspace's (unprivileged) account
```

Let an agent work, bookmark what the host should see, and pull it back:

```bash
jj bookmark create wip -r @        # in the sandbox
devbox jj fetch                    # on the host
jj new wip@devbox-my-workspace
```

`devbox stop` shuts the box down (keeping its disk); `devbox destroy` tears it
down.

**Sharing one box between workspaces:**

```bash
devbox box create dev --vcpus 8 --memory 32768    # or --provider ec2/aws
# in each workspace's marker:  box = "dev"
cd ~/src/binius && devbox create                  # attaches as user `binius`
cd ~/src/web    && devbox create                  # attaches as user `web`
devbox box list --attached
```

**Several repos in one workspace** — put the marker beside them:

```bash
cd ~/src/acme                 # holds frontend/ and backend/
$EDITOR devbox.toml           # two [[repos]] entries, mode = "push"
devbox create                 # one account holding both checkouts
devbox jj fetch               # from inside either one: fetches that one
```

## Concepts

A **box** is a VM. A **workspace** is a directory on your host holding one or
more repos, marked by a devbox config file. Several workspaces can share a box,
each in its own unprivileged account.

| | **box** | **workspace** |
| --- | --- | --- |
| is | a machine: provider, resources, image | an account, its checkouts, creds, mods |
| named | `dev`, `ec2-dev` (canonical name) | the marker directory's basename |
| created by | `devbox box create dev [flags]` | `devbox create` (attaches it to its box) |
| configured by | **command-line flags only** | `devbox.toml` or `.devbox/config.toml` |

A workspace names its box with `box = "dev"`; without it, the workspace gets a
box of its own, named after itself.

**The marker.** Two spellings, same keys:

```
~/src/devbox/              ~/src/acme/
  .devbox/                   devbox.toml
    config.toml              mods/rust.sh
    mods/rust.sh             frontend/          <- a repo
  devbox/                    backend/           <- a repo
```

- `<repo>/.devbox/config.toml` — the workspace **is** one repo. Git-exclude it
  (`.git/info/exclude`).
- `<root>/devbox.toml` — the workspace **holds** repos as children of the root.

devbox walks up from the working directory (or `-C PATH`) for either; nearest
wins, and a directory holding both is an error.

**Mods.** A workspace changes its account through **mods**, named in its
`mods = [...]` list. Every workspace also runs a built-in base set: jj, dotfiles,
Claude Code, GitHub auth, and repo bootstrap. A mod comes in one of two kinds,
and a name can have both:

- **Nix mods** (`<name>.nix`) are home-manager modules, built into the agent's
  generation. They are declarative, pinned, shared through the box's nix store,
  and can be reapplied safely. **Prefer them.**
- **Script mods** (`<name>.sh`) are idempotent bash scripts the provisioning
  driver runs. They are the escape hatch for what a package in the store can't
  do: apt packages, system state (`runas = ["root"]`), or a tool that has to
  update itself in the account's home, like the Claude Code and Codex installers.

Both kinds live in the workspace's mods directory (or a global or built-in one),
and `devbox sync --mods` reapplies them to a live box. See [Mods](#mods).

**Accounts.** Every box has one privileged account and one unprivileged account
per attached workspace. Agents run as the latter.

| | **privileged** (`--user`, default `admin`) | **agent** (`agent_user`, default = workspace name) |
| --- | --- | --- |
| sudo | NOPASSWD `ALL` | **none**, and no root-equivalent group |
| holds | the provisioning payload and logs | the checkouts, `~/.claude`, its `[env]` creds |
| SSH | `devbox box ssh <box>` | `devbox ssh` |
| shell | stock bash, unconfigured vim | zsh + the workspace's home-manager generation |

What this buys: the agent can't tamper with the system or the provisioner, or
read another account's home. What it **doesn't**: the agent still has network
egress and its own tokens — this contains system integrity, not credential
exfiltration. The cost: **the agent cannot install system packages**; add them
to a mod's `packages` and run `devbox sync --mods`. The trade-offs of sharing a
box are in [ARCHITECTURE.md](ARCHITECTURE.md#accounts-and-isolation).

> **Never grant the agent partial sudo.** `sudo apt-get`, `sudo systemctl`,
> `sudo pip` or membership in `docker`/`lxd` are each root. The driver refuses
> to continue if the agent has acquired any of them.

An existing account with no devbox ownership record is refused; claim it with
`devbox create --adopt`. One owned by another workspace is refused outright.

## Commands

**Box commands** take a box name.

| Command | Behavior |
|---|---|
| `devbox box create NAME [flags]` | Launch a box (bare name for libvirt, `<provider>-NAME` for `--provider ec2\|aws`) and run the box playbook. Idempotent. Flags: `--provider --user --vcpus --memory --disk --instance-type --arch --ami --spot/--on-demand --bundle --blueprint --zone`. |
| `devbox box list [--attached]` (= `devbox list`) | All devbox boxes across providers. `--attached` adds a WORKSPACES column (one SSH per box). |
| `devbox box up NAME` / `box stop NAME` | Start / gracefully shut down. |
| `devbox box ssh NAME [-- args]` | SSH in as the privileged user — the box's only sudo shell. |
| `devbox box status NAME` | cloud-init status and the tail of the box's provision log. |
| `devbox box sync --mods` | Reconverge the box playbook. |
| `devbox box destroy NAME` | Delete the box and its disk; the prompt names every attached workspace. |

**Workspace commands** act on this workspace's account on its box.

| Command | Behavior |
|---|---|
| `devbox create [--adopt]` | Ensure the box exists (booting it if the workspace owns it), then attach. Re-running reconverges. |
| `devbox ssh [-- args]` | SSH in as this workspace's account. |
| `devbox status` | cloud-init progress, this workspace's provision log, failed systemd user units. |
| `devbox ip` | The box's IPv4. |
| `devbox sync --envvars --mods --files` | Any combination, run in that order: rewrite the `[env]` files, re-run the workspace playbook, rsync `[[sync]]` files. `devbox create` runs all three. |
| `devbox jj fetch [REPO]` | Fetch a checkout's bookmarked work: the named repo, else the one you're inside, else the only one. |
| `devbox host` | Refresh the SSH aliases and print this workspace's. |
| `devbox herdr [-- args]` | `herdr --remote` into this workspace's account (herdr runs on the host). |
| `devbox detach [--purge]` | Give up the account: drop its ownership record, key, creds and nix profile; the home stays, so `create` re-attaches. `--purge` deletes the account. |
| `devbox destroy` | Destroy the box — refused if another workspace is attached. |

**Host commands**: `devbox nix check [--build] [--out-dir DIR]` evaluates the
home-manager modules without a box (see [DEVELOPMENT.md](DEVELOPMENT.md)).

Every subcommand accepts `-C/--workspace-dir PATH` and `--box NAME`, given
before the subcommand.

SSH aliases live in `~/.config/devbox/ssh/` (one file each, pulled into
`~/.ssh/config` by an `Include` devbox adds): `devbox-<box>-admin` for the
privileged account, and `devbox-<box>-<workspace>` per attachment — just
`devbox-<box>` when the workspace owns the box.

Mods reconverge in place with `devbox sync --mods`. Changes to devbox's
first-boot `init-script.sh` only reach a box through `box destroy` + `box
create`.

## Configuration

### Global: `~/.config/devbox/config.toml`

Optional; every key has a default.

```toml
libvirt_uri = "qemu:///system"
network = "default"
cache_dir = "/var/tmp/devbox"   # base image cache; not $HOME (qemu must read it)

[base_image]
url = "https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img"

[defaults]                      # box create fallbacks
vcpus = 6
memory_mib = 12288
disk_gb = 100

[ssh]
pubkey_path = "~/.ssh/id_ed25519.pub"

[jj]                            # default: the host's jj user.name/user.email
name = "Jim Posen"
email = "jim.posen@gmail.com"

[env]                           # creds for every box (see Credentials)
GITHUB_TOKEN = "github_pat_..."

[aws]                           # enables the ec2 and aws providers
region = "us-east-1"
profile = "dev"

[ec2]                           # optional EC2 defaults
default_security_groups = ["sg-..."]
default_subnets = ["subnet-a", "subnet-b"]   # tried in order
spot = true
```

Commands that talk to AWS check credentials first; for an expired SSO profile on
a TTY, devbox offers to run `aws sso login`.

### Workspace: the marker file

Every key is optional; an empty file is a valid marker.

```toml
# workspace = "my-workspace"  # default: the directory's basename
# box = "dev"                 # default: the workspace's own name
# agent_user = "my-workspace" # default: the workspace name; a legal, non-system username
# mods = ["rust"]             # appended to the base playbook (see Mods)

[[repos]]                     # one per checkout; none = no repo bootstrap
name = "my-repo"              # omit only under .devbox/, meaning "the root is the repo"
mode = "clone"                # "clone" (box clones url) | "push" (empty repo to push into)
url = "https://github.com/<org>/my-repo"   # clone mode only
# host = "my-repo"            # host path under the root (default: name)
# dir = "my-repo"             # box path, ~/<dir> (default: name)

# [env]                       # overrides the global [env] per key
# GITHUB_TOKEN = "github_pat_..."
```

All of a workspace's repos land in one account and share its credentials. Adding
one to a live workspace is a `devbox sync --mods`.

### Boxes

Hardware is a `devbox box create` flag, never workspace config; devbox errors on
`vcpus`, `provider`, `[ec2]` etc. in a marker.

```bash
devbox box create dev --vcpus 8 --memory 32768 --disk 200
devbox box create dev --provider ec2 --instance-type g5.xlarge --arch x86_64 [--ami ami-... --spot]
devbox box create dev --provider aws --bundle xlarge_3_0 [--blueprint ubuntu_24_04 --zone us-east-1b]
```

Cloud box names get a provider prefix — `box create dev --provider ec2` makes
`ec2-dev`, which is what a workspace's `box =` holds. Names are unique across
providers.

**Lightsail (`--provider aws`)**: a bundle fixes CPU/RAM/disk (`--disk` is
ignored; default `xlarge_3_0`, 4 vCPU / 16 GB / 320 GB); x86 only; Ubuntu 24.04
blueprint by default; dynamic public IP, re-resolved every command; port 22 open
by default; no spot.

### Credentials (`[env]`)

```toml
[env]
GITHUB_TOKEN = "github_pat_..."                       # the agent's
REGISTRY_TOKEN = { value = "...", owner = "admin" }   # the privileged account's only
```

Each entry goes to one account, as `export` lines in its 0600
`~/.config/devbox/env`, sourced by every shell (interactive or not). A workspace's
`[env]` overrides the global one per key; prefer it for anything scoped, since a
global token reaches every workspace on a shared box.

To keep secrets out of the marker, put them in a dotenv-style `secrets.env` next
to it (beside `devbox.toml`, or inside `.devbox/`). It wins over `[env]` on a key
clash. Gitignore it.

**GitHub auth** is `GITHUB_TOKEN`: `gh` reads it, and git/jj HTTPS use it through
gh's credential helper. Use a fine-grained PAT scoped to the repos the box needs.

## Host→box file sync

`devbox sync --files` rsyncs host-local, untracked files (typically agent skills)
to the box. Host wins: box-side edits inside a synced subtree get overwritten.

```toml
[[repos]]
name = "frontend"
mode = "push"
[[repos.sync]]
src = ".claude/skills"         # that checkout on both sides -> ~/frontend/.claude/skills

[[sync]]
src = "$HOME/.claude/skills"   # $HOME/~ resolved per side
include = ["jujutsu"]          # only these children, each mirrored on its own

[[sync]]
src = "shared-notes"           # workspace root -> ~/shared-notes (devbox.toml markers only)
```

`dst` defaults to `src`. With `include`, each named child is its own mirror, so
box-side siblings outside the list are never deleted.

## Exchanging commits with the sandbox

The box is an SSH git remote, through this workspace's alias.

**Fetch.** jj exports only bookmarked commits, so the agent bookmarks what the
host should see; `devbox jj fetch` refreshes the alias, ensures a jj remote at
`ssh://<alias>/home/<agent_user>/<dir>`, and fetches.

**Push** (`mode = "push"`) seeds an empty repo on the box:

```bash
devbox host                       # prints the alias
jj git remote add sandbox ssh://<alias>/home/<agent_user>/<dir>
jj bookmark set main -r @ --allow-backwards   # must be "main"
jj git push --remote sandbox --allow-new -b main
```

The box repo uses `receive.denyCurrentBranch=updateInstead`: a push updates its
working tree only for the checked-out `main`, and is refused if that tree is
dirty. Don't force it — check what the agent left uncommitted.

## Mods

Everything after first boot is a **mod**: a home-manager module (preferred), an
idempotent bash script, or both, under one name.

```
box playbook:        box-packages → nix
workspace playbook:  home-manager → github-auth → jj → dotfiles → neovim →
                     claude-code → cargo-binstall → repo-bootstrap
                     + your mods = [...]
```

A name resolves **workspace > global > built-in**, from

```
<workspace mods dir>/          # mods/ beside devbox.toml, or .devbox/mods/
~/.config/devbox/mods/
devbox/mods/                   # built-in
```

`<name>.sh` is a step the driver runs; `<name>.nix` is a module in the agent's
home-manager generation, enabled by `config.devbox.mods.<name>.enable`. Each
suffix overrides only its own suffix, so to replace a built-in module ship your
own `<name>.nix` (`{ ... }: { }` turns it off). Every `.nix` file in a mods
directory must be a home-manager module. Never put an `[env]` value in a module;
read `~/.config/devbox/env` at run time.

A script declares metadata in a fenced TOML block after the shebang. All keys
are optional:

```bash
#!/usr/bin/env bash
#
# /// mod
# requires = ["jj", "github-auth"]   # must appear earlier; validated, never reordered
# runas = ["admin", "agent"]         # default ["agent"]; also "root"
# packages = ["jq", "unzip"]         # apt-installed by the driver before the body
# ///
```

`runas` also decides scope: a mod with an `agent` target runs only as the agent
in a workspace playbook (and as its other targets in the box playbook); one
without runs as all its targets. A mod body has **no sudo** — use `packages` or
`runas = ["root"]`. A root mod in a workspace changes the whole shared box, and
devbox warns about it.

Context arrives in the environment (`DEVBOX_AGENT_USER`, `DEVBOX_ADMIN_USER`,
`DEVBOX_JJ_NAME`, `DEVBOX_WORKSPACE`, …) and the owning account's `[env]` is
sourced. The checkouts are in `$DEVBOX_PAYLOAD_DIR/repos`, one
`<name>\t<mode>\t<dir>\t<url>` line each.

Logs land in the privileged account's home: `~/provision-<agent_user>.log`, and
per mod under `~/provision/<agent_user>/`.

### Example: a rust workspace

```toml
mods = ["rust"]
```

```nix
# .devbox/mods/rust.nix   (or mods/rust.nix beside devbox.toml)
{ config, lib, pkgs, ... }:

let
  install = pkgs.writeShellScript "devbox-rust-install" ''
    set -eu
    export PATH="${lib.makeBinPath [ pkgs.rustup ]}:$PATH"
    rustup toolchain install stable --profile default --component rust-analyzer
    rustup default stable
  '';
in
lib.mkIf config.devbox.mods.rust.enable {
  home.packages = [ pkgs.rustup pkgs.sccache ];

  home.file.".cargo/config.toml".text = ''
    [build]
    rustc-wrapper = "${lib.getExe pkgs.sccache}"
  '';

  # Runs on every switch; rustup is idempotent.
  home.activation.devboxRust = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    run ${install}
  '';
}
```

Packages and config files come from the store. The activation hook covers what
a store can't hold, here the toolchain rustup manages in `~/.rustup`. If the
build also needs system libraries, add a small script mod with the same name
next to it, for example `rust.sh` holding only
`# /// mod` / `# packages = ["clang", "libssl-dev"]` / `# ///`.

`devbox sync --mods` applies it to a live box. The opt-in built-ins are `codex`,
`nodejs`, `uv`, `cloudflared`, `claude-github-mcp` and `claude-linear-mcp`; each
file's header says what it does.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [DEVELOPMENT.md](DEVELOPMENT.md)

