# devbox

A tool for launching project-scoped development virtual machines. Its primary
use cases are sandboxed agentic development, and cross-platform development and
benchmarking on cloud VMs.

devbox gives you an easy-to-use CLI for creating, managing, and interacting with
those machines, and ships with an agent skill.

It currently supports three VM providers: **libvirt** (local VMs), **ec2**
(AWS EC2), and **aws** (AWS Lightsail — flat-rate bundles, simpler than EC2).

## Basics & Architecture

Every devbox starts from the latest Ubuntu LTS as its base image — currently
Ubuntu 26.04 "Resolute Raccoon" — brought to life on first boot by a single
bash **init script** handed to the instance as user-data (just the privileged
user, baseline packages, `BASH_ENV` wiring, and the hostname). Everything else — jj, dotfiles, Claude Code, the repo,
a project's toolchain — is provisioned by composable, idempotent **mods** run
**over SSH** after boot, so a box can be reconverged in place (`devbox sync
--mods`) instead of rebuilt.

### Two accounts per box

A box has two users, and AI agents run as the one without privilege:

| | **privileged** (`user`, default `$USER`) | **agent** (`agent_user`, default = the project name) |
| --- | --- | --- |
| sudo | NOPASSWD `ALL` | **none**, and in no group that grants it |
| home | `/home/<user>`, `0750` | `/home/<agent_user>`, `0750` |
| holds | the provisioning payload, `~/provision.log` | the repo checkout, `~/.claude`, `~/.local/bin`, the `[env]` creds |
| SSH | `devbox ssh --admin` | `devbox ssh` (the default) |
| created by | the init script, at first boot | the provisioning driver, so it reconverges |

**What this buys, and what it doesn't.** A sudoless agent still has full network
egress and still holds `GITHUB_TOKEN` / `LINEAR_API_KEY` — which for most threat
models *are* the crown jewels. This is containment of **system integrity**, not
of **credential exfiltration**. What it does gain: the agent can't tamper with
the provisioner or the system, can't read the privileged user's home, can't
persist outside its own, and has a crisply defined blast radius.

> **Never grant the agent partial sudo.** `NOPASSWD: /usr/bin/apt-get` is
> equivalent to full root — `apt-get install ./evil.deb` runs maintainer scripts
> as root, and `-o APT::Update::Pre-Invoke=…` runs arbitrary commands. The same
> goes for `sudo systemctl`, `sudo pip`, or membership in `docker`/`lxd`. If any
> of those get added, the isolation is decorative and should be described that
> way. The provisioning driver asserts this on every run and refuses to continue
> if the agent has picked up sudo or a root-equivalent group.

The trade-off is real and deliberate: **the agent cannot install system
packages.** Where it used to `sudo apt-get install` its way out of a missing
build dependency mid-task, it now needs a `# packages:` line on a mod and a
`devbox sync --mods`. First boot installs `build-essential` and `pkg-config` to
cover the common case.

**Migrating an existing box: recreate it.** There is no in-place upgrade. A
`devbox sync --mods` against a box built under the old single-user model will
create the agent account and move the canonical checkout to its home, but
nothing moves the old tree — it stays in the privileged user's home, orphaned and
no longer synced or fetched. The driver warns when it sees one. Land any work in
flight first (`devbox jj fetch`), then `destroy` + `create`.

Once a box is up, the way you work with it —
`ssh`/`exec`/`list`/`provision`/`jj fetch`/`sync` — is backend-independent; only
`create`/`up`/`stop`/`destroy` and address discovery are provider-specific (a
thin `Provider` seam). All projects share one cached base image (libvirt), one
global config, and one base provisioning playbook of idempotent "mods" (jj,
dotfiles, Claude Code, GitHub auth, repo bootstrap) that devbox owns; each
project appends only what's different via a `mods = [...]` list. `devbox` finds
"which instance belongs to the project in the current working directory" the way
`git`/`direnv`/`mise` locate their own markers.

## Example Workflow

Mark a directory as a devbox project by dropping a `.devbox/config.toml` at its
root. A minimal local (libvirt) project just needs to say how to get the code:

```toml
# .devbox/config.toml
[repo]
mode = "clone"
dir = "my-project"
url = "https://github.com/<org>/my-project"
```

Then, from anywhere inside the project:

```bash
devbox create      # download the base image (cached), boot the VM, provision it
                   #   over SSH (mods + [[sync]] files) — blocks until ready
devbox ssh         # log into the sandbox
```

Work in the sandbox (e.g. let an agent make changes), bookmark what should be
visible to the host, and pull it back:

```bash
# in the sandbox:
jj bookmark create wip -r @
# on the host:
devbox jj fetch                # fetch the sandbox's bookmarked work
jj new wip@devbox-my-project   # check it out locally
```

`devbox stop` shuts the box down (keeping its disk); `devbox destroy` tears it
down entirely. Launching a cloud box instead is the same flow with `devbox
create --provider ec2 --name my-project-arm` (or `--provider aws` for
Lightsail) — see [Per-project setup](#per-project-setup) for the `[ec2]` /
`[lightsail]` config.

## Usage

| Command | Behavior |
|---|---|
| `devbox create` | Launch the project's default libvirt VM (bare-named = the project), or a named cloud instance with `--provider ec2\|aws --name <n>`. Renders the init script and first-boots the instance with it as user-data (libvirt NoCloud seed / EC2 `UserData` / Lightsail `userData`), and stamps the instance (libvirt `<metadata>` / AWS tags). Errors if the name already exists anywhere (global-uniqueness). |
| `devbox list` | List devbox-managed instances — name, provider, project, state, resources, uptime (cloud rows show the EC2 instance type / Lightsail bundle and a `($)` cost flag while running). Global (doesn't need a project); unions libvirt (this host, by `<metadata>` stamp) + EC2 + Lightsail (account-wide, by `devbox:managed` tag). |
| `devbox up` | Start an already-defined, shut-off domain. Errors if undefined — run `create` first. |
| `devbox stop` | Gracefully shut down (ACPI) a running domain. |
| `devbox destroy` | Tear down the domain and its overlay disk (the cached base image is left alone). |
| `devbox ip` | Print the domain's IPv4. |
| `devbox ssh [-- args...]` | `ssh` into the domain as the **unprivileged agent user** — where the repo checkout and tooling live. `devbox ssh --admin` logs in as the privileged user instead; that's the box's only sudo shell, for debugging the machine itself. |
| `devbox status` | SSH in (as the privileged user, whose home holds the log) and show `cloud-init status --long` plus the tail of `~/provision.log`. |
| `devbox sync --envvars` | (Re-)write the box's devbox-managed 0600 `[env]` file — scoped creds/vars (`GITHUB_TOKEN`, API keys) into its shell env. Full rewrite each run (drops keys removed from config). See "Credentials" below. |
| `devbox sync --mods[=a,b,c]` | (Re-)run the provisioning **mods** over SSH against a running box — reconverge (bump jj, add a plugin, re-apply a project's rust mod) without destroy+create. Covers base **and** project mods. Idempotent; tees to `~/provision.log` (per-mod logs in `~/provision/`). No list runs the whole playbook; a comma-separated list runs just those (fast iteration). |
| `devbox sync --files` | Push host-local, non-VCS files (`[[sync]]`/`[[repo.sync]]`) onto the box via rsync — agent skills, git-excluded project files. Re-runnable + incremental. See "Host→box file sync" below. |
| `devbox jj fetch` | jj-fetch the instance's bookmarked work into your local repo: refreshes the SSH alias (address via the provider — libvirt or EC2), ensures the `devbox-<name>` jj remote, and runs `jj git fetch`. See "Exchanging commits" below. |
| `devbox host` | Refresh the SSH aliases used for `jj fetch` (below); prints the agent alias, and names the `-admin` one. |

`devbox sync` requires at least one of `--envvars`/`--mods`/`--files` (any
combination). They run in that order — env before mods (a mod may need a cred,
e.g. a private clone), files last (dsts can depend on the repo being cloned).
It's the same steps `devbox create` runs on a fresh box.

Each instance gets **two** SSH aliases: `devbox-<name>` for the agent (the one
`jj fetch` and `--files` use, and the name every existing jj remote already
carries) and `devbox-<name>-admin` for the privileged user (`--envvars` and
`--mods`, both of which need sudo).

Every subcommand accepts `-C/--project-dir PATH` and `--name NAME` (both
before the subcommand, so `ssh`'s trailing args don't swallow them) — `-C`
resolves a project other than the current directory; `--name` targets a
specific instance instead of the project's libvirt default.

**Changing the first-boot init script (the packages/login user/`BASH_ENV` in
devbox's `init-script.sh`) only takes effect via `destroy` + `create`, never
`up`** — user-data runs once, on first boot (on libvirt, cloud-init's NoCloud
seed ISO is only attached then), so an already-created instance never re-runs
it. The
provisioning **mods**, by contrast, reconverge in place: `devbox sync --mods`
re-delivers the driver + mods + env over SSH and re-runs them on a live box, so
edits to a mod (or to the context it consumes — jj identity, GitHub token,
`[repo]`, a project's `mods` list) apply without a rebuild.

### Project & instance resolution

Instance-operating subcommands first find the **project** (except `list`,
which is global and skips this):

1. Start at `-C PATH` if given, else the current directory.
2. Walk upward (like `git` searching for `.git`) looking for
   `.devbox/config.toml` at each directory level.
3. The first directory that has it wins.
4. No marker found up to `/` → error telling you to pass `-C` or add a
   `.devbox/config.toml` to the project.

Then they resolve **which instance** within it: an explicit `--name` wins;
otherwise the project's libvirt default (bare-named = the project). Daily use
(one local VM per project) never needs `--name`.

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
   disk_gb = 60

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
   spot = true                          # optional default market (projects can override)
   ```

   `[aws]` holds the shared AWS account credentials and is what enables **both**
   AWS-backed providers (**ec2** and **aws**/Lightsail); `[ec2]` holds the
   EC2-specific knobs (both sections optional). Lightsail has no global section
   at all — its knobs are all per-project, under `[lightsail]`. Omit them
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
   devbox's provisioning (the built-in `jj` mod) always runs
   `jj config set --user user.name/user.email` in the sandbox, using
   whichever source resolved.

   `[env]` is a flat map of environment variables — scoped credentials, API
   keys — injected into every box (see [Credentials](#credentials-env) below).
   A project's `[env]` overrides it per-key. **`GITHUB_TOKEN` is the way to give
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

## Per-project setup

Create a `.devbox/config.toml` at the project's root — **not committed**;
add `.devbox/` to the project's `.git/info/exclude` (local-only, invisible
to other contributors — there's nothing project-shareable in here, it's
purely this machine's VM config):

```toml
# project = "my-project"    # a label on this project's instances
#                           # (default: the directory's basename)
# provider = "libvirt"      # default provider for a bare `devbox create`
# user = "jimpo"            # the PRIVILEGED account (sudo). Default: host $USER
# agent_user = "my-project" # the UNPRIVILEGED account agents run as (no sudo).
#                           # Default: the project name. Must be a legal Linux
#                           # username, differ from `user`, and not be a system
#                           # or cloud-image account — validated up front.
# vcpus, memory_mib, disk_gb optionally override the global [defaults]
# mods = ["rust"]           # provisioning mods appended after the base playbook
#                           # (see "Provisioning: mods" below)

[repo]                      # optional; omit entirely for no repo bootstrap
mode = "clone"               # "clone" | "push"
dir = "my-project"           # created at ~/<dir> in the sandbox
url = "https://github.com/<org>/<repo>"   # required iff mode == "clone"

# [env]                     # creds/vars injected into the box's shell env;
# GITHUB_TOKEN = "github_pat_..."   #   overrides the global [env] per-key. A
#                           #   repo-scoped PAT belongs here (.devbox is git-excluded).

# Host->box file sync (rsync). [[sync]] = $HOME/~/absolute-anchored (resolved
# per side); [[repo.sync]] = repo-relative (host: marker dir; guest: ~/<repo.dir>).
# See "Host->box file sync" below.
# [[sync]]
# src = "$HOME/.claude/skills"   # dst defaults to src (resolved per side)
# include = ["jujutsu"]          # only these children of src (each mirrored)
# [[repo.sync]]
# src = ".claude/skills"         # -> ~/<repo.dir>/.claude/skills on the box

[ec2]                       # optional; EC2 launch defaults for --provider ec2
instance_type = "g5.xlarge"  # required if [ec2] is present
architecture = "x86_64"      # "x86_64" | "arm64" (drives the AMI lookup)
# ami = "ami-0123..."        # optional: pin an explicit AMI (skips the lookup)
# ami_name_filter = "..."    # optional: override the Ubuntu AMI name glob
# spot = true                # optional: override the global [ec2].spot default
# interruption_behavior = "stop"   # spot only: stop | terminate | hibernate
# disk_gb (above) doubles as the EBS root volume size on EC2

[lightsail]                 # optional; launch defaults for --provider aws
                            #   (Lightsail). Every key has a default, so the
                            #   section can be omitted entirely.
# bundle = "xlarge_3_0"     # optional: pin an explicit Lightsail bundle id.
                            #   Default: xlarge_3_0 — 16 GB RAM / 4 vCPU /
                            #   320 GB SSD (~$84/mo). x86 only: Lightsail has
                            #   no ARM bundles, so there is no `architecture`.
# blueprint = "ubuntu_24_04"        # optional: the OS image. Default = the
                            #   newest Ubuntu LTS blueprint Lightsail offers,
                            #   which trails the 26.04 libvirt/ec2 userland.
# availability_zone = "us-east-1b"  # optional; default = <[aws].region> + "a"
# disk_gb does NOT apply here — a Lightsail bundle fixes CPU/RAM/disk together
```

A **project** is a label, not a machine: it can have several instances. The
default one is a libvirt domain **bare-named = the project**, which `devbox
create` (no args) makes. Cloud instances always carry an explicit name:
`devbox create --provider ec2 --name binius-arm`.

Instance names encode the provider so every command can route to the right
backend from the name alone — no API call or credential load just to find an
instance's owner. The scheme:

- **Canonical name** = `<provider>-<name>` for cloud providers, **bare** for
  libvirt (local is the common case, and "bare = the project's libvirt
  default" is worth keeping). So `create --provider ec2 --name binius-arm`
  composes **`ec2-binius-arm`** — that's the identity `devbox list` shows and
  that every later command references: `devbox --name ec2-binius-arm ssh`
  queries EC2 even from a libvirt-default project.
- `--provider` is **create-only**; `project.provider` is purely the
  bare-`create` default. Per-instance commands (`ip`/`ssh`/`status`/`stop`/
  `up`/`destroy`/`provision`/`host`/`jj fetch`) derive the provider by parsing the
  name: the first `-`-delimited segment routes to that provider if it's a known
  keyword (`ec2`, `aws`, …), else libvirt.
- The `<provider>-` prefix is **added automatically** — pass the bare name to
  `--name` (`binius-arm`, not `ec2-binius-arm`). devbox rejects a `--name` (or
  a project name, for the bare libvirt case) that already starts with a known
  provider prefix, so a name can't misparse.
- Provider-side, the prefix is redundant, so the **AWS-side name drops it**:
  `ec2-binius-arm` shows as `devbox-binius-arm` in the EC2 console, and
  `aws-binius-arm` is created as the Lightsail instance `binius-arm`. devbox's
  own `devbox:name` addressing tag keeps the canonical prefixed name on both.

Instance names are globally unique across providers (the prefix makes that fall
out for free). Every config key is optional; an empty `config.toml` is a valid
marker. `name` is no longer a field — devbox errors if it's present, to flag
the migration.

cfleet's "instance templates" (`instance_type`/`architecture`/AMI/`spot`) fold
into this `[ec2]` section; the same re-runnable provisioning (jj, dotfiles,
Claude Code, `[repo]` bootstrap) runs on EC2 over SSH after boot, so a
sandbox is identical whether it's a local libvirt VM or an EC2 instance.

The **aws** provider is AWS **Lightsail**: the same init script as user-data and
the same provisioning, but flat-rate *bundles* instead of an instance type plus an
EBS volume. Trade-offs worth knowing:

- **Bundles fix the machine.** A bundle sets CPU, RAM, and disk together, so
  `disk_gb` is ignored on this provider. The default, `xlarge_3_0`, is 16 GB RAM
  / 4 vCPU / 320 GB SSD (~$84/mo). Set `bundle` to pin any other tier. There is
  no `architecture` knob: Lightsail's Linux bundles are **x86-only** (no
  ARM/Graviton tier), unlike `[ec2]`.
- **The OS trails a little.** `blueprint` defaults to `ubuntu_24_04`, the newest
  Ubuntu LTS blueprint Lightsail offers — an older userland than the Ubuntu
  26.04 cloud image the libvirt and ec2 providers boot. The mods provision the
  same way on it; pin `blueprint` if you want something else.
- **Dynamic public IPv4.** No static IP is allocated (so none is left behind to
  pay for or release); `devbox ip`/`ssh` re-resolve the current address on every
  command, which is what makes a post-`stop`/`up` address change a non-event.
- **Port 22 is open by default** on a Lightsail instance, so there are no
  security groups to configure — hence no global `[lightsail]` section.
- **No spot market:** `--spot`/`--on-demand` are EC2-only and are ignored here.

`devbox create --provider aws --name binius-box` needs nothing but `[aws]`
creds; a project can select it with `provider = "aws"` and no `[lightsail]`
section at all.

> **On IMDS and instance roles.** devbox attaches **no** instance profile on
> either AWS provider, so there are no role credentials to steal at
> `169.254.169.254` today. If you ever add one, note that the unprivileged agent
> reaches the metadata endpoint exactly as easily as root does — an instance
> role would hand it AWS credentials outright, straight past the account split.
> Block IMDS for the agent user (an iptables owner-match rule on that address) in
> the same change that attaches the profile.

`[repo]` drives how the sandbox gets your code, via the built-in
`repo-bootstrap` mod (see below) — no project needs to author this by hand:

- **`mode = "clone"`** — the project already has a git remote. The sandbox
  runs `jj git clone <url> ~/<dir>` while provisioning.
- **`mode = "push"`** — brand-new project with nothing pushed anywhere yet.
  The sandbox creates an empty colocated jj+git repo at `~/<dir>`, ready to
  receive a push from the host — see "Exchanging commits" below. `url` is not used
  (and not allowed) in this mode.
- **Omit `[repo]` entirely** (or `mode = "none"`) — no repo bootstrap at all,
  e.g. a scratch/experiment VM.

A project adds everything it needs as a **mod** (see below). There is no
per-project first-boot hook: devbox's first-boot layer is one fixed init script
(see [First boot](#first-boot-the-init-script)), and the older
`.devbox/cloud-config.yaml` fragment is **gone** — port anything you had there
to a project mod, which reconverges in place instead of forcing a rebuild.
Provisioning context (jj identity, `[repo]`, the GitHub token) flows to mods via
the environment, not `%TOKEN%`s.

## Provisioning: mods

Provisioning is a list of small, idempotent, composable bash scripts — **mods**
(inspired by Ansible roles, without Ansible). A mod installs/configures one
thing (jj, dotfiles, Claude Code, a project's rust toolchain, …). A project's
effective **playbook** is the built-in base plus the project's own `mods`,
appended:

```
base (built-in, fixed order):  jj → github-auth → dotfiles-packages → dotfiles →
                               vim → claude-code → repo-bootstrap
project mods (config.toml):    + rust + wasm-tools + …
```

Base ordering encodes the constraints (jj + github-auth before repo-bootstrap
so a private `jj git clone` can authenticate; `dotfiles-packages`, the root half,
before `dotfiles`, the per-user half). The 95% case is "just add rust":
`mods = ["rust"]`.

**Resolution** — each mod name resolves to a `<name>.sh` script by precedence
**project > global > built-in**, so a project can shadow a built-in by name:

```
mods/*.sh                     # built-in, at the repo root
~/.config/devbox/mods/*.sh    # global user (shared across projects)
.devbox/mods/*.sh             # project-local
```

**Static files, env for context.** Mods are static (lintable, shellcheck-able,
diffable, and directly runnable — the metadata below is comment headers, so the
shebang stays on line 1) and receive context via the **environment**
(`DEVBOX_AGENT_USER`, `DEVBOX_ADMIN_USER`, `DEVBOX_JJ_NAME`, `DEVBOX_REPO_URL`,
…), not `%TOKEN%` substitution. Each mod also sources its own target user's
`[env]` creds file (see [Credentials](#credentials-env)), so a mod that needs a
secret — e.g. `repo-bootstrap` cloning a private repo with `GITHUB_TOKEN` — has
it, and it never lands in a script on disk, in a command line, or in a log.

**A mod body runs without sudo.** The driver runs as the privileged user and
drops to the agent for each mod, because configuring a user's home directory is
what a mod almost always does. So `sudo` inside a mod is a bug — use the two
headers below instead. This is also why the payload carries no secrets: it has to
be readable by the account the driver drops into.

**Who a mod runs as (`# runas:`).** Default `agent`. Also `admin` (the privileged
account) and `root`. A mod may name more than one and then runs once per target —
`dotfiles` is `# runas: admin agent`, so `devbox ssh --admin` gets a usable shell
too. Validated before any boot/SSH; an unknown target is a hard error.

**System packages (`# packages:`).** `# packages: jq unzip` — apt-installed by
the driver, as the privileged user, before the body runs. This is the sanctioned
route now that a mod body has no sudo. The index is refreshed once per run.

**Delivered over SSH** (one path). devbox tars the resolved driver + mods +
context + manifest, pipes it to the box over the `devbox-<name>-admin` SSH alias
(the driver needs sudo), extracts it into a per-run temp dir, and runs the
driver. This is the **only** delivery path — the same for a fresh box and a
reconverge:

- **`devbox create`** — after boot (once the init script has created the
  privileged user and installed packages), creates the agent account, injects the
  `[env]` creds, runs the mods, then pushes `[[sync]]` files. `create` blocks
  through all of it, so when it returns the box is fully provisioned.
- **`devbox sync --mods[=a,b,c]`** — reconverge a live box; no list runs the
  whole playbook, a list runs just those (fast iteration while writing a mod).

The init script carries **no** provisioning payload — it only does base setup.
(Trade-off: the host must stay attached during `create`; if it disconnects
mid-provision, re-run `devbox sync --mods`.)

The driver runs each mod in playbook order, stops at the first failure (naming
which mod and which account), and tees each mod's output to
`~/provision/<name>.log` — `~/provision/<name>.<user>.log` for a mod that runs
for more than one account — plus the combined `~/provision.log` that `devbox
status` tails. All of these live in the **privileged** user's home, since that
is where the driver runs.

Before any mod, on every run, the driver ensures the box's users: it creates the
agent account if absent (no supplementary groups, `0750` home, locked password,
your SSH key authorized), locks both homes down, writes each account's
`~/.bash_env`, and **refuses to continue** if the agent has somehow acquired sudo
or a root-equivalent group. It is a driver preamble rather than a mod on purpose:
it must always run, must not be shadowable by a project's `.devbox/mods/`, and
must not be skippable via `devbox sync --mods=jj`.

**Idempotency contract:** every mod either overwrites unconditionally or guards
on "already done", so `create` and every reconverge are safe. Each built-in
mod's header documents which pattern it uses.

**Declaring dependencies (`# requires:`):** a mod that depends on another running
first declares it with a header comment — `# requires: jj github-auth` (names
space/comma-separated; repeat the line to add more, they're unioned). devbox
parses this statically and, at playbook-resolution time (before any boot/SSH),
hard-errors if a required mod is missing from the effective playbook or listed
*after* the mod that needs it. It is **not** a resolver — it never reorders or
auto-inserts; it just validates order (so a future reorder can't silently break
things). The `# requires:` line holds **only** dep names — no trailing inline
comment (the parse runs to end of line). Built-in examples: `repo-bootstrap`
`# requires: jj github-auth`, `vim` `# requires: dotfiles`.

### Example: a rust project

`.devbox/config.toml` gains one line, and the rust setup is a project-local mod
— nothing else to configure:

```toml
# .devbox/config.toml
mods = ["rust"]
```

```bash
# .devbox/mods/rust.sh
#!/usr/bin/env bash
#
# packages: clang libssl-dev
#
# No `# runas:` — it defaults to the agent, whose $HOME this installs into.
# No `sudo` either: the body has none. Packages go in the header above, where
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

`devbox create` renders it by literal `%TOKEN%` substitution (not `{{...}}`,
which collides with cloud-init's jinja): `%DEVBOX_USER%`, `%DEVBOX_SSH_PUBKEY%`
and `%DEVBOX_HOSTNAME%` (the instance's canonical name). What it does, as root,
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
   table and the box has two accounts — a per-user path there would make one
   user source the other's file. (`BASH_ENV=$HOME/…` is not an option: pam_env
   reads `/etc/environment` as plain `KEY=VALUE`, so the expansion has to happen
   in bash.)
4. Adds the official GitHub CLI apt repo, then installs the baseline the mods
   lean on: `zsh vim python3 curl git git-lfs ca-certificates gh rsync
   build-essential pkg-config`. (The repo is needed because `gh` isn't in every
   Ubuntu release's archive — notably 24.04, the newest LTS blueprint Lightsail
   offers — and under `set -e` a missing package would abort first boot.) No
   distro upgrade.
5. Switches the login shell to `zsh` now that it exists.

It deliberately does **not** create the agent user. User-data runs once, at first
boot, so an account born here could never appear on an existing box and changing
`agent_user` would need a rebuild — the provisioning driver creates it instead,
where it reconverges.

Every step is idempotent, and the whole thing carries no provisioning payload —
mods run over SSH afterwards. A project cannot extend this layer (the
`.devbox/cloud-config.yaml` fragment is gone); project-specific setup is a mod,
which has the advantage of reconverging on a live box.

## Credentials (`[env]`)

`[env]` is a flat map of environment variables — scoped credentials, API keys —
hardcoded in `config.toml` and injected into a box. Global `[env]`
(`~/.config/devbox`) applies to every box; project `[env]` (`.devbox`, which is
git-excluded) adds project-specific keys and **overrides the global map
per-key**.

```toml
[env]
GITHUB_TOKEN = "github_pat_..."
SOME_API_KEY = "..."
```

**In the guest**, devbox writes a single managed 0600 file in the **agent's**
home — `/home/<agent_user>/.config/devbox/env` (`export KEY=val` lines) — and
sources it from **both** that user's `~/.zshenv` (interactive zsh) **and** its
`~/.bash_env` (the `BASH_ENV` target). The second one matters: Claude Code's Bash
tool runs *non-interactive bash*, which doesn't read `.zshenv` — so creds only in
`.zshenv` would be invisible to the agent and anything it runs.

The agent's home is the **only** copy. The privileged user can still read it via
sudo — it's the trusted side of the box — but nothing writes a second copy, and a
mod running as `admin` or `root` therefore sees no credentials at all. A mod that
needs one should be an agent mod (as `repo-bootstrap` and `github-auth` are).

**GitHub auth** is just `GITHUB_TOKEN` in `[env]`: `gh` reads it natively (no
`gh auth login`, so nothing lands in `~/.config/gh/hosts.yml`), and the
`github-auth` mod runs `gh auth setup-git` so `git`/`jj git` HTTPS use gh's
credential helper — private clones and pushes work.

**Delivery & idempotency:** `devbox sync --envvars` ships the map over SSH (piped,
never echoed to a log), rewriting the managed file in full each run — so removing
a key from config removes it from the box. Auto-run first on `devbox create`.

**Security:** plaintext in `config.toml` (host) and the 0600 file (guest) — an
accepted trade-off for scoped creds on a personal VM. The real change from a
credential-helper store is that values now live in the **process environment** of
everything the agent runs (the point), so they're visible via `/proc/<pid>/environ`
to the dev user. Keep them out of provision logs (devbox delivers them piped, not
substituted).

## Host→box file sync

`devbox sync --files` pushes **host-local, non-VCS-tracked files** onto the box —
primarily agent **skills** (which change often) from wherever they live on the
host (`~/.claude/skills`, a project's git-excluded skills) to wherever they go
on the box. This is a distinct problem from provisioning: a **mod runs on the
box and can't read host-local files**, so host-only files fundamentally need a
host-side push. (Skills that live in the project repo arrive via `[repo]`; skills
published on GitHub belong in a mod that clones them box-side — `--files` is only
for host-local files.)

Configure it with two arrays of `{ src, dst?, include? }` entries:

```toml
# repo-relative: host = the marker dir (repo root), guest = the checkout
# ~/<repo.dir>. Requires a [repo] with mode != "none".
[[repo.sync]]
src = ".claude/skills"                 # -> ~/<repo.dir>/.claude/skills on the box

# $HOME/~/absolute-anchored, resolved per side (host home reading src, guest
# home writing dst).
[[sync]]
src = "$HOME/.claude/skills"
include = ["jujutsu"]                   # only this child of src, not every skill
```

- **`dst` defaults to `src`**, and `$HOME`/`~` resolve **per side** — so the
  common "same relative location both ends" case needs only `src`. Only
  `$HOME`/`~` are supported (not arbitrary env vars — guest-side expansion would
  mean running a shell on the box); both sides resolve offline.
- **`include`** narrows to named children of `src` (names only for now). Its key
  subtlety is that it **defines the mirror unit**: an entry with `include`
  expands to one rsync per child (`src/jujutsu → dst/jujutsu`), each mirrored
  (`--delete`) within its *own* subtree — so `include = ["jujutsu"]` never
  `--delete`s box-installed skills outside the list. Without `include`, the whole
  `src` is one mirror of `src → dst`.

**Mechanism:** `rsync` over the same `devbox-<name>` SSH alias, idempotent and
incremental (cheap to run often). **Host wins** — a mirror deletes host-removed
files within a synced subtree, so an agent's box-side edits to a synced skill
are clobbered on the next sync (acceptable for host-authored skills).

**When it runs:** on demand via `devbox sync --files`, and automatically as the
last step of `devbox create` (after the mods run, since the sync dsts depend on
the repo clone being in place). Files and mods are separate flags of `devbox
sync` precisely because skills change on a faster cadence than provisioning —
`devbox sync --files` alone is cheap. (`rsync` must be installed on the host; it
ships in the box's base packages.)

## Exchanging commits with the sandbox (jj)

The sandbox is a plain SSH-reachable git remote for the host, in both
directions: the host **fetches** the agent's work to view/test it, and, for
projects with no git server yet (`[repo] mode = "push"`), the host **pushes**
to seed the sandbox with an initial commit. This is **identical whether the
instance is libvirt, EC2, or Lightsail** — the address is resolved through the
provider (libvirt DHCP lease, or the cloud instance's public IP); everything
above that is the same
jj-remote model. `[repo]` is project-level, so any instance in the project
(`--name` to pick one) is fetched the same way.

Both directions go through the `devbox-<name>` SSH alias, which `devbox jj fetch`
and `devbox host` set up/refresh:

- A dedicated known-hosts file, `~/.config/devbox/ssh/known_hosts`,
  **truncated on every `devbox create`** (a freshly created instance has a
  brand-new SSH host key, so stale entries can only cause false mismatches).
- A `Host devbox-<name>` block in `~/.config/devbox/ssh/<name>.conf` with the
  instance's current IP (from the provider), SSH user, and
  `StrictHostKeyChecking accept-new` scoped to that dedicated known-hosts file
  — regenerated on every call since the address can change across `stop`/`up`
  cycles (a libvirt DHCP lease, or a new public IP after stop/start — Lightsail
  boxes use a dynamic IPv4, so this is expected there rather than exceptional).
- A one-time, idempotent `Include ~/.config/devbox/ssh/*.conf` line in
  `~/.ssh/config`.

### Fetch: pulling sandbox commits to the host (`[repo] mode = "clone"`)

The sandbox already cloned the project on first boot (via `[repo]`), so
there's no manual clone step. The agent just bookmarks work it wants visible
on the host — jj only exports *bookmarked* commits to git refs, which is what
the host's fetch reads:

```bash
# Sandbox side (agent), per unit of work that should become visible to the host:
jj bookmark create wip -r @

# Host side (from your local jj checkout): one command, any provider.
devbox jj fetch                      # refreshes the alias, wires the jj remote, fetches
jj new wip@devbox-my-project         # check out the fetched bookmark
```

`devbox jj fetch` refreshes the SSH alias (address via the provider), ensures a jj
git remote named after the alias (`devbox-<name>`) points at the instance's
colocated repo (`ssh://devbox-<name>/home/<agent_user>/<dir>`), and runs `jj git
fetch`. It's equivalent to the manual flow:

```bash
devbox host                                             # prints alias, e.g. devbox-my-project
jj git remote add devbox-my-project ssh://devbox-my-project/home/<agent_user>/<dir>
jj git fetch --remote devbox-my-project
```

### Push: seeding a brand-new sandbox (`[repo] mode = "push"`)

For a project with no git remote yet, the sandbox's common provisioning
creates an *empty* colocated jj+git repo at `~/<dir>` on first boot
(`git init --initial-branch=main`, `jj git init --colocate`, then
`git config receive.denyCurrentBranch updateInstead` — the setting that lets
a push update a non-bare repo's checked-out working tree). The host seeds it:

```bash
devbox host                                             # prints alias, e.g. devbox-my-project-dev
jj git remote add sandbox ssh://devbox-my-project-dev/home/<agent_user>/<dir>
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

The code is a `devbox/` package: `config` (global/project config), `provision`
(modular mods — resolution, env context, payload/driver assembly), `initscript`
(first-boot user-data script render), `sync` (host→box rsync file sync),
`cli` (subcommands + argparse), and a `providers/` package — a `Provider` seam
(`providers/base.py`) with `libvirt`, `ec2`, and `lightsail` (the `aws`
provider) implementations. Data files
(`init-script.sh`, `provision-driver.sh`) live inside the package; the built-in
`mods/*.sh` live at the repo root.
