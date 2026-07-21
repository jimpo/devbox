# devbox

A global, multi-project dev-sandbox VM manager (libvirt + cloud-init). All
projects share one cached base VM image, one global config, and one common
cloud-config template (dotfiles, jj, Claude Code, base packages) that devbox
owns; each project supplies only what's different via an optional
`.devbox/cloud-config.yaml`, merged on top. `devbox` finds "which VM belongs
to the project in the current working directory" the way `git`/`direnv`/`mise`
locate their own markers.

## Requirements

- Debian/Ubuntu/Pop!_OS with libvirt + qemu:
  ```
  sudo apt install virtinst libvirt-daemon-system qemu-system-x86 ubuntu-cloudimage-keyring
  ```
  Run as a user in the `libvirt` group (so `qemu:///system` works without
  sudo), or invoke `devbox` with sudo. `ubuntu-cloudimage-keyring` provides
  Canonical's signing key at `/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg`,
  used by `devbox create` to verify the base image's checksum (see below).
- [`uv`](https://docs.astral.sh/uv/) — manages devbox's install and runtime
  environment (including its one dependency, `pyyaml`, used to merge the
  common cloud-config template with each project's fragment).

## Install

```bash
uv tool install --editable .
```

This installs a `devbox` command into `~/.local/bin` (must be on `$PATH`),
backed by a `uv`-managed virtual environment under
`~/.local/share/uv/tools/devbox`. `--editable` means edits to `devbox.py`
take effect immediately, without reinstalling. To update after pulling new
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
   ```

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
   session `gh auth login` happens to have cached on the host. An unresolved
   GitHub token is never fatal: it's only used to wire up `gh auth login` +
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
```

A **project** is a label, not a machine: it can have several instances. The
default one is a libvirt domain **bare-named = the project**, which `devbox
create` (no args) makes. `--name` targets a specific instance for any command;
without it, commands act on the project's libvirt default. (Named/cloud
instances arrive with the AWS provider — for now libvirt is the only provider
and each project has just its one bare default.) Every config key is optional;
an empty `config.toml` is a valid marker. `name` is no longer a field — devbox
errors if it's present, to flag the migration.

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

## Usage

| Command | Behavior |
|---|---|
| `devbox create` | Download/verify the shared base image if needed (cached globally), render + merge the cloud-config (common template + project fragment), define + first-boot the domain (bare-named = the project), and stamp it with devbox `<metadata>`. Errors if a domain with this name is already defined (global-uniqueness) — run `destroy` first. |
| `devbox list` | List devbox-managed instances on this host — name, provider, project, state, resources, uptime. Global (doesn't need to be run inside a project); filters libvirt domains by the devbox `<metadata>` stamp. |
| `devbox up` | Start an already-defined, shut-off domain. Errors if undefined — run `create` first. |
| `devbox stop` | Gracefully shut down (ACPI) a running domain. |
| `devbox destroy` | Tear down the domain and its overlay disk (the cached base image is left alone). |
| `devbox ip` | Print the domain's IPv4. |
| `devbox ssh [-- args...]` | `ssh` into the domain. |
| `devbox status` | SSH in and show `cloud-init status --long` plus the tail of `~/provision.log`. |
| `devbox provision` | Re-run the provisioning script (`provision-common.sh`) over SSH against a running domain — reconverge (add a package, bump jj, add a plugin) without destroy+create. Idempotent; tees to `~/provision.log`. |
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

## jj sync: exchanging commits with the sandbox

The sandbox VM is a plain SSH-reachable git remote for the host, in both
directions: the host **fetches** the agent's work to view/test it, and, for
projects with no git server yet (`[repo] mode = "push"`), the host **pushes**
to seed the sandbox with an initial commit. `devbox host` sets up/refreshes,
for either direction:

- A dedicated known-hosts file, `~/.config/devbox/ssh/known_hosts`,
  **truncated on every `devbox create`** (a freshly created domain has a
  brand-new SSH host key, so stale entries can only cause false mismatches).
- A `Host devbox-<name>` block in `~/.config/devbox/ssh/<name>.conf` with the
  domain's current IP, resolved SSH user, and `StrictHostKeyChecking
  accept-new` scoped to that dedicated known-hosts file — regenerated on
  every `devbox host` call since the DHCP lease can change across
  `stop`/`up` cycles.
- A one-time, idempotent `Include ~/.config/devbox/ssh/*.conf` line in
  `~/.ssh/config`.

### Fetch: pulling sandbox commits to the host (`[repo] mode = "clone"`)

The sandbox already cloned the project on first boot (via `[repo]`), so
there's no manual clone step. The agent just needs to bookmark work it wants
visible on the host — jj only exports *bookmarked* commits to git refs,
which is what the host's fetch reads:

```bash
# Sandbox side (agent), per unit of work that should become visible to the host:
jj bookmark create wip -r @

# Host side, one-time:
devbox host                                             # prints alias, e.g. devbox-my-project-dev
jj git remote add sandbox ssh://devbox-my-project-dev/home/<user>/<dir>
# Host side, ongoing:
devbox host                                             # refresh alias (IP/host key may differ)
jj git fetch --remote sandbox
jj new wip@sandbox
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
