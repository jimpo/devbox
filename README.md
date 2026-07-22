# devbox

A tool for launching project-scoped development virtual machines. Its primary
use cases are sandboxed agentic development, and cross-platform development and
benchmarking on cloud VMs.

devbox gives you an easy-to-use CLI for creating, managing, and interacting with
those machines, and ships with an agent skill.

It currently supports two VM providers: **libvirt** (local VMs) and **AWS**
(EC2).

## Basics & Architecture

Every devbox starts from the latest Ubuntu LTS as its base image — currently
Ubuntu 26.04 "Resolute Raccoon" — customized on first boot with
[cloud-init](https://cloud-init.io/). Provisioning beyond first boot is a
standalone, re-runnable shell script, so a box can be reconverged in place
(`devbox provision`) instead of rebuilt.

Once a box is up, the way you work with it —
`ssh`/`exec`/`list`/`provision`/`sync` — is backend-independent; only
`create`/`up`/`stop`/`destroy` and address discovery are provider-specific (a
thin `Provider` seam). All projects share one cached base image (libvirt), one
global config, and one common cloud-config + provisioning script (dotfiles, jj,
Claude Code, base packages) that devbox owns; each project supplies only what's
different via an optional `.devbox/cloud-config.yaml`, merged on top. `devbox`
finds "which instance belongs to the project in the current working directory"
the way `git`/`direnv`/`mise` locate their own markers.

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
devbox status      # watch cloud-init progress until it reports "done"
devbox ssh         # log into the sandbox once it's ready
```

Work in the sandbox (e.g. let an agent make changes), bookmark what should be
visible to the host, and pull it back:

```bash
# in the sandbox:
jj bookmark create wip -r @
# on the host:
devbox sync                    # fetch the sandbox's bookmarked work
jj new wip@devbox-my-project   # check it out locally
```

`devbox stop` shuts the box down (keeping its disk); `devbox destroy` tears it
down entirely. Launching a cloud box instead is the same flow with `devbox
create --provider aws --name my-project-arm` — see [Per-project
setup](#per-project-setup) for the `[aws]` config.

## Usage

| Command | Behavior |
|---|---|
| `devbox create` | Launch the project's default libvirt VM (bare-named = the project), or a named cloud instance with `--provider aws --name <n>`. Renders + merges the cloud-config, first-boots via cloud-init (libvirt NoCloud seed / EC2 user-data), and stamps the instance (libvirt `<metadata>` / EC2 tags). Errors if the name already exists anywhere (global-uniqueness). |
| `devbox list` | List devbox-managed instances — name, provider, project, state, resources, uptime (AWS rows show the instance type and a `($)` cost flag while running). Global (doesn't need a project); unions libvirt (this host, by `<metadata>` stamp) + AWS (account-wide, by `devbox:managed` tag). |
| `devbox up` | Start an already-defined, shut-off domain. Errors if undefined — run `create` first. |
| `devbox stop` | Gracefully shut down (ACPI) a running domain. |
| `devbox destroy` | Tear down the domain and its overlay disk (the cached base image is left alone). |
| `devbox ip` | Print the domain's IPv4. |
| `devbox ssh [-- args...]` | `ssh` into the domain. |
| `devbox status` | SSH in and show `cloud-init status --long` plus the tail of `~/provision.log`. |
| `devbox provision` | Re-run the provisioning script (`provision-common.sh`) over SSH against a running domain — reconverge (add a package, bump jj, add a plugin) without destroy+create. Idempotent; tees to `~/provision.log`. |
| `devbox sync` | jj-fetch the instance's bookmarked work into your local repo: refreshes the SSH alias (address via the provider — libvirt or AWS), ensures the `devbox-<name>` jj remote, and runs `jj git fetch`. See "jj sync" below. |
| `devbox host` | Refresh the SSH alias used for jj-sync (below); prints the alias name. |

Every subcommand accepts `-C/--project-dir PATH` and `--name NAME` (both
before the subcommand, so `ssh`'s trailing args don't swallow them) — `-C`
resolves a project other than the current directory; `--name` targets a
specific instance instead of the project's libvirt default.

**Changing a project's cloud-config (common template or `.devbox/cloud-config.yaml`)
only takes effect via `destroy` + `create`, never `up`** — cloud-init's
NoCloud seed ISO is only attached on first boot, so an already-defined
domain never re-reads it. The exception is the provisioning body
(`provision-common.sh`): `devbox provision` re-runs it over SSH on a live
box, so edits to *that* script (and to the tokens it consumes — jj identity,
GitHub token, `[repo]`) reconverge without a rebuild. Changes to the parts
that live in `common-cloud-config.yaml` itself (packages, the user, YAML
structure) still need `destroy` + `create`.

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
- For the **AWS** provider (optional): configured AWS credentials (e.g. a named
  profile) with EC2 permissions. `boto3` ships as a devbox dependency; the
  `[aws]` global-config section enables it.
- [`uv`](https://docs.astral.sh/uv/) — manages devbox's install and runtime
  environment (its dependencies: `pyyaml` for the cloud-config merge, `boto3`
  for the AWS provider).

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

   [github]
   token = "ghp_..."

   [aws]                                # optional — only needed for --provider aws
   region = "us-east-1"                 # required if [aws] is present
   profile = "dev"                      # optional AWS named profile
   default_security_groups = ["sg-..."] # optional
   default_subnets = ["subnet-a", "subnet-b"]  # optional; tried in order (capacity fallback)
   spot = true                          # optional default market (projects can override)
   ```

   `[aws]` is optional and enables the AWS/EC2 provider. Omit it entirely on a
   libvirt-only host — `devbox list` then never reaches for AWS credentials.
   Single region/profile only for now (multi-region/profile deferred). `boto3`
   is a declared dependency, so it's always installed, but only touched when you
   actually use AWS.

   `[jj]` is optional — if unset, `devbox create` falls back to the host's
   own `jj config get user.name`/`user.email` (which you almost certainly
   already have set, if you use jj). It's only truly required if that
   fallback also fails (no `jj` on the host, or no identity configured
   there either), in which case `devbox create` dies with a clear message.
   devbox's common cloud-config template always runs
   `jj config set --user user.name/user.email` in the sandbox, using
   whichever source resolved.

   `[github]` is also optional, but **has no host fallback** (unlike
   `[jj]`) — deliberately: use a
   [fine-grained personal access token](https://github.com/settings/personal-access-tokens)
   scoped to just the repos the sandbox needs, not whatever broad-scoped
   session `gh auth login` happens to have cached on the host. A project's
   `.devbox/config.toml` can set its own `[github].token`, which **overrides**
   this global one (project → global → none) — the natural home for a PAT
   scoped to a single repo, since `.devbox/` is git-excluded/local-only. An
   unresolved GitHub token is never fatal: it's only used to wire up `gh auth login` +
   `gh auth setup-git` in the sandbox (so `git`/`jj git`/`gh` against
   `github.com` authenticate transparently), which is only needed for
   private repos or pushing back to GitHub from inside the sandbox. Cloning
   a public repo works fine with no token at all — `devbox create` just
   prints a note and moves on if it's unset. The token is embedded directly
   in the rendered cloud-config (same mechanism as the SSH pubkey/jj
   identity) and ends up in plaintext inside the sandbox's `gh` config — an
   accepted trade-off for a personal, single-user VM (mitigated by scoping
   the PAT narrowly in the first place), same as this project's Docker
   sandbox counterpart already does with `GITHUB_PERSONAL_ACCESS_TOKEN`.

   There's no `sha256` field: `devbox create` fetches `SHA256SUMS` +
   `SHA256SUMS.gpg` from alongside the image URL, verifies the signature
   against Canonical's cloud-image signing key (`gpgv` +
   `ubuntu-cloudimage-keyring`, from Requirements above), and uses the
   checksum it lists. This avoids pinning a hash by hand — `.../release/...`
   URLs are mutable (Canonical overwrites the file in place on respins), so
   a hand-pinned checksum inevitably goes stale and breaks `create` until
   someone updates it.
2. **SSH `Include`** — `devbox host` (see "jj sync" below) appends `Include
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
# user, vcpus, memory_mib, disk_gb optionally override the global [defaults]

[repo]                      # optional; omit entirely for no repo bootstrap
mode = "clone"               # "clone" | "push"
dir = "my-project"           # created at ~/<dir> in the sandbox
url = "https://github.com/<org>/<repo>"   # required iff mode == "clone"

# [github]                  # optional; overrides the global [github].token
# token = "ghp_..."         #   for this project — set a PAT scoped to just
#                           #   this repo. Unset here falls back to global.

[aws]                       # optional; EC2 launch defaults for --provider aws
instance_type = "g5.xlarge"  # required if [aws] is present
architecture = "x86_64"      # "x86_64" | "arm64" (drives the AMI lookup)
# ami = "ami-0123..."        # optional: pin an explicit AMI (skips the lookup)
# ami_name_filter = "..."    # optional: override the Ubuntu AMI name glob
# spot = true                # optional: override the global [aws].spot default
# interruption_behavior = "stop"   # spot only: stop | terminate | hibernate
# disk_gb (above) doubles as the EBS root volume size on AWS
```

A **project** is a label, not a machine: it can have several instances. The
default one is a libvirt domain **bare-named = the project**, which `devbox
create` (no args) makes. Cloud instances always carry an explicit name:
`devbox create --provider aws --name binius-arm`.

Instance names encode the provider so every command can route to the right
backend from the name alone — no API call or credential load just to find an
instance's owner. The scheme:

- **Canonical name** = `<provider>-<name>` for cloud providers, **bare** for
  libvirt (local is the common case, and "bare = the project's libvirt
  default" is worth keeping). So `create --provider aws --name binius-arm`
  composes **`aws-binius-arm`** — that's the identity `devbox list` shows and
  that every later command references: `devbox --name aws-binius-arm ssh`
  queries AWS even from a libvirt-default project.
- `--provider` is **create-only**; `project.provider` is purely the
  bare-`create` default. Per-instance commands (`ip`/`ssh`/`status`/`stop`/
  `up`/`destroy`/`provision`/`host`/`sync`) derive the provider by parsing the
  name: the first `-`-delimited segment routes to that provider if it's a known
  keyword (`aws`, …), else libvirt.
- The `<provider>-` prefix is **added automatically** — pass the bare name to
  `--name` (`binius-arm`, not `aws-binius-arm`). devbox rejects a `--name` (or
  a project name, for the bare libvirt case) that already starts with a known
  provider prefix, so a name can't misparse.
- Provider-side, the prefix is redundant, so the **AWS console `Name` tag drops
  it**: `aws-binius-arm` shows as `devbox-binius-arm` in EC2. devbox's own
  addressing tag keeps the canonical `aws-binius-arm`.

Instance names are globally unique across providers (the prefix makes that fall
out for free). Every config key is optional; an empty `config.toml` is a valid
marker. `name` is no longer a field — devbox errors if it's present, to flag
the migration.

cfleet's "instance templates" (`instance_type`/`architecture`/AMI/`spot`) fold
into this `[aws]` section; the same re-runnable provisioning (jj, dotfiles,
Claude Code, `[repo]` bootstrap) runs on EC2 via cloud-init user-data, so a
sandbox is identical whether it's a local libvirt VM or an EC2 instance.

`[repo]` drives how the sandbox gets your code, via devbox's common
cloud-config template (see below) — no project needs to author this by hand:

- **`mode = "clone"`** — the project already has a git remote. The sandbox
  runs `jj git clone <url> ~/<dir>` on first boot.
- **`mode = "push"`** — brand-new project with nothing pushed anywhere yet.
  The sandbox creates an empty colocated jj+git repo at `~/<dir>`, ready to
  receive a push from the host — see "jj sync" below. `url` is not used
  (and not allowed) in this mode.
- **Omit `[repo]` entirely** (or `mode = "none"`) — no repo bootstrap at all,
  e.g. a scratch/experiment VM.

An optional `.devbox/cloud-config.yaml` alongside it adds project-specific
packages/scripts on top of the common template — see "How cloud-config
merging works" below. Most projects need this for language-toolchain setup
(Rust via rustup, etc.) that isn't common to every devbox project. It's
entirely optional: if a project needs nothing beyond dotfiles/jj/Claude
Code/base packages, skip this file.

Both files support the same `%DEVBOX_USER%`/`%DEVBOX_SSH_PUBKEY%` (plus
`%DEVBOX_JJ_NAME%`/`%DEVBOX_JJ_EMAIL%`/`%DEVBOX_GITHUB_TOKEN%`/
`%DEVBOX_REPO_MODE%`/`%DEVBOX_REPO_DIR%`/`%DEVBOX_REPO_URL%`) placeholders
as the common template, filled in by literal text substitution before
parsing (not `{{...}}`, which collides with cloud-init's own jinja syntax
and with valid YAML flow-mapping syntax).

### How cloud-config merging works

devbox ships its own `common-cloud-config.yaml` (packages, the dev user, the
`BASH_ENV` wiring) plus a standalone, re-runnable `provision-common.sh` (jj
install + identity, GitHub auth (`gh`) + Claude Code `github`/`linear`
plugins, dotfiles, and the `[repo]`-driven clone/push bootstrap). `devbox
create` substitutes the script's `%TOKEN%`s, injects it into the seed's
`write_files`, and merges the template with the project's optional
`.devbox/cloud-config.yaml`. Because the provisioning body is a standalone
idempotent script, `devbox provision` can re-run the exact same script over
SSH against an already-booted box (reconverge) without a destroy+create.

- `%TOKEN%` substitution happens on each file's raw text independently,
  *before* either is parsed as YAML.
- Cloud-config keys that are lists — `packages`, `write_files`, `runcmd`,
  `bootcmd`, `users`, `groups` — are **concatenated common-then-project**.
  Order matters: common's `runcmd` installs jj/dotfiles/`~/.local/bin`
  before any project-specific step that might assume they already exist.
  Don't redeclare `users:` or the repo bootstrap in a project fragment —
  those are owned by the common template.
- Every other top-level key is a **whole-value override** — if the project
  fragment sets it, it replaces the common template's value entirely (no
  recursive dict merging).
- `hostname` defaults to the project's `name` unless either file sets it.
- The merged result gets a fresh `#cloud-config` header prepended (YAML
  parsing drops comments, so the original magic first line doesn't survive
  a parse→merge→dump round trip on its own).

## jj sync: exchanging commits with the sandbox

The sandbox is a plain SSH-reachable git remote for the host, in both
directions: the host **fetches** the agent's work to view/test it, and, for
projects with no git server yet (`[repo] mode = "push"`), the host **pushes**
to seed the sandbox with an initial commit. This is **identical whether the
instance is libvirt or AWS** — the address is resolved through the provider
(libvirt DHCP lease or EC2 public IP); everything above that is the same
jj-remote model. `[repo]` is project-level, so any instance in the project
(`--name` to pick one) syncs the same way.

Both directions go through the `devbox-<name>` SSH alias, which `devbox sync`
and `devbox host` set up/refresh:

- A dedicated known-hosts file, `~/.config/devbox/ssh/known_hosts`,
  **truncated on every `devbox create`** (a freshly created instance has a
  brand-new SSH host key, so stale entries can only cause false mismatches).
- A `Host devbox-<name>` block in `~/.config/devbox/ssh/<name>.conf` with the
  instance's current IP (from the provider), SSH user, and
  `StrictHostKeyChecking accept-new` scoped to that dedicated known-hosts file
  — regenerated on every call since the address can change across `stop`/`up`
  cycles (a libvirt DHCP lease, or a new EC2 public IP after stop/start).
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
devbox sync                          # refreshes the alias, wires the jj remote, fetches
jj new wip@devbox-my-project         # check out the fetched bookmark
```

`devbox sync` refreshes the SSH alias (address via the provider), ensures a jj
git remote named after the alias (`devbox-<name>`) points at the instance's
colocated repo (`ssh://devbox-<name>/home/<user>/<dir>`), and runs `jj git
fetch`. It's equivalent to the manual flow:

```bash
devbox host                                             # prints alias, e.g. devbox-my-project
jj git remote add devbox-my-project ssh://devbox-my-project/home/<user>/<dir>
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
jj git remote add sandbox ssh://devbox-my-project-dev/home/<user>/<dir>
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

The code is a `devbox/` package: `config` (global/project config), `cloudconfig`
(cloud-config render + provisioning script), `cli` (subcommands + argparse), and
a `providers/` package — a `Provider` seam (`providers/base.py`) with `libvirt`
and `aws` implementations. Data files (`common-cloud-config.yaml`,
`provision-common.sh`) live inside the package.
