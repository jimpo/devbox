# devbox

A global, multi-project dev-sandbox VM manager (libvirt + cloud-init). Each
project supplies its own cloud-init config; all projects share one cached
base VM image and one global config. `devbox` finds "which VM belongs to the
project in the current working directory" the way `git`/`direnv`/`mise`
locate their own markers.

## Requirements

- Debian/Ubuntu/Pop!_OS with libvirt + qemu:
  ```
  sudo apt install virtinst libvirt-daemon-system qemu-system-x86
  ```
  Run as a user in the `libvirt` group (so `qemu:///system` works without
  sudo), or invoke `devbox` with sudo.
- [`uv`](https://docs.astral.sh/uv/) — `devbox` has no external Python
  dependencies, but `uv` manages its install and runtime environment.

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
   sha256 = "0c9fb915bab0b36b361d3bf8aeae2115dda19d81a306656964de048033481670"

   [defaults]
   vcpus = 6
   memory_mib = 12288
   disk_gb = 60

   [ssh]
   pubkey_path = "~/.ssh/id_ed25519.pub"
   ```
2. **SSH `Include`** — `devbox host` (see "jj sync" below) appends `Include
   ~/.config/devbox/ssh/*.conf` to `~/.ssh/config` the first time it runs, so
   there's nothing to do here manually; it's idempotent.

## Per-project setup

Commit a `.devbox.toml` marker at the project's root:

```toml
name = "my-project-dev"                     # libvirt domain name
cloud_init = "cloud-init/user-data.yaml"    # path relative to this file
# user, vcpus, memory_mib, disk_gb optionally override the global [defaults]
```

Your cloud-init file can use two placeholders, filled in by literal text
substitution before `devbox` hands the file to `virt-install` (not
`{{...}}`, which collides with cloud-init's own jinja syntax and with valid
YAML flow-mapping syntax):

- `%DEVBOX_USER%` → host `$USER` (or this marker's `user =` override)
- `%DEVBOX_SSH_PUBKEY%` → contents of the pubkey file from the global config

An optional, gitignored `.devbox.local.toml` sibling may override `name` (or
any other field) — the escape hatch for a sibling checkout/clone of the same
project that needs its *own* VM instead of sharing the one named in the
committed marker.

## Usage

| Command | Behavior |
|---|---|
| `devbox create` | Download/verify the shared base image if needed (cached globally), template the project's cloud-init file, define + first-boot the domain. Errors if a domain with this name is already defined — run `destroy` first. |
| `devbox up` | Start an already-defined, shut-off domain. Errors if undefined — run `create` first. |
| `devbox stop` | Gracefully shut down (ACPI) a running domain. |
| `devbox destroy` | Tear down the domain and its overlay disk (the cached base image is left alone). |
| `devbox ip` | Print the domain's IPv4. |
| `devbox ssh [-- args...]` | `ssh` into the domain. |
| `devbox status` | SSH in and show `cloud-init status --long` plus the tail of `~/provision.log`. |
| `devbox host` | Refresh the SSH alias used for jj-sync (below); prints the alias name. |

Every subcommand accepts `-C/--project-dir PATH` (before the subcommand) to
resolve a project other than the current directory.

**Changing a project's cloud-init file only takes effect via `destroy` +
`create`, never `up`** — cloud-init's NoCloud seed ISO is only attached on
first boot, so an already-defined domain never re-reads it.

### Project resolution

Every subcommand resolves "which VM" the same way:

1. Start at `-C PATH` if given, else the current directory.
2. Walk upward (like `git` searching for `.git`) looking for
   `.devbox.local.toml` then `.devbox.toml` at each directory level.
3. The first directory with either marker wins; if both files are present
   there, `.devbox.local.toml` overrides `name` (and any other fields it
   sets) from the committed `.devbox.toml`.
4. No marker found up to `/` → error telling you to pass `-C` or add a
   marker file.

## jj sync: pulling sandbox commits to the host

The sandbox VM is a plain SSH-reachable git remote that the host fetches
from — one-directional today (the agent writes in the sandbox; the host
pulls to view/test). `devbox host` sets up/refreshes:

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

End-to-end workflow once a project's VM is created:

```bash
# Sandbox side (agent), one-time per project: use a colocated jj repo, not a
# plain `git clone` - jj only exports *bookmarked* commits to git refs,
# which is what the host's fetch reads.
jj git clone https://github.com/<org>/<repo> ~/<repo>
# Sandbox side, per unit of work that should become visible to the host:
jj bookmark create wip -r @

# Host side, one-time:
devbox host                                             # prints alias, e.g. devbox-my-project-dev
jj git remote add sandbox ssh://devbox-my-project-dev/home/<user>/<repo>
# Host side, ongoing:
devbox host                                             # refresh alias (IP/host key may differ)
jj git fetch --remote sandbox
jj new wip@sandbox
```

## Development

```bash
uv sync              # create .venv with dev dependencies (pytest)
uv run pytest        # run the test suite
```
