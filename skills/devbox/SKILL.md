---
name: devbox
description: devbox is a multi-project dev-sandbox VM manager for local libvirt VMs and AWS EC2 / Lightsail instances, provisioned by composable idempotent "mods". Covers what it is, its command set, and how to configure a project (`.devbox/config.toml`: repo bootstrap, mods, EC2, Lightsail) plus the global `~/.config/devbox/config.toml`. Use when asked to set up devbox for a project, create/manage a dev sandbox VM, or when devbox subcommands (create/up/ssh/provision/jj fetch/…) come up.
allowed-tools: Bash(devbox *), Bash(jj *), Bash(git *), Bash(gh *), Read, Write, Edit, AskUserQuestion
---

# devbox

A multi-project dev-sandbox VM manager for three providers — **libvirt** (local
VMs), **ec2** (AWS EC2), and **aws** (AWS Lightsail — flat-rate bundles, no
security groups to configure). All projects share one cached base image and a base
playbook of idempotent provisioning **mods** (jj, dotfiles, Claude Code, GitHub
auth, repo bootstrap) that devbox owns; each project adds only what's different
via `.devbox/config.toml`. Full reference: `README.md` in this repo.

## Commands

`-C PATH` (project dir) and `--name NAME` (target a specific instance) go
*before* the subcommand.

| Command | Behavior |
|---|---|
| `devbox create` | Boot + provision the project's default libvirt VM, or a named cloud instance with `--provider ec2\|aws --name <n>`. |
| `devbox list` | All devbox-managed instances (libvirt on this host + EC2/Lightsail account-wide): name, provider, state, resources. |
| `devbox up` / `stop` / `destroy` | Start / graceful-shutdown / tear down (destroy also deletes the disk). |
| `devbox ip` / `ssh [-- args]` / `status` | Print IP / SSH in / show cloud-init progress + `~/provision.log`. |
| `devbox jj fetch` | jj-fetch the instance's bookmarked work into your local repo (refreshes the SSH alias + `devbox-<name>` jj remote, runs `jj git fetch`). |
| `devbox sync --envvars` | (Re-)write the box's 0600 `[env]` file — scoped creds/vars (`GITHUB_TOKEN`, API keys) into its shell env. Full rewrite each run. |
| `devbox sync --mods[=a,b]` | Run provisioning mods over SSH on a live box (reconverge, no rebuild). No list = whole playbook; else just those. |
| `devbox sync --files` | Push host-local non-VCS files (`[[sync]]`/`[[repo.sync]]`) onto the box via rsync (agent skills, git-excluded files). |
| `devbox host` | Refresh/print the `devbox-<name>` SSH alias used for `jj fetch`. |

Instances: the libvirt default is **bare-named = the project**; cloud instances
take an explicit `--name` and get a canonical `<provider>-<name>` (e.g.
`ec2-foo`, `aws-foo`) — use that in later commands. `--provider` is create-only
(the provider is derived from the name).

## Configuration

### Global — `~/.config/devbox/config.toml` (optional; everything has a default)

- `[jj] name`/`email` — sandbox identity. Falls back to the host's own `jj
  config get user.name`/`user.email`, so usually no setup. If `devbox create`
  dies for lack of it, ask the user rather than guessing.
- `[env]` — a flat map of env vars (creds/API keys) injected into every box's
  shell env. **`GITHUB_TOKEN` here is how the box gets GitHub auth** (private
  clones/pushes); `gh`/`git`/`jj git` read it. A project's `[env]` overrides
  per-key. Unset is fine for public repos.
- `[aws] region` (+ optional `profile`) — the shared AWS account creds; enables
  **both** the `ec2` and `aws` (Lightsail) providers. `[ec2]` (optional
  `default_security_groups`, `default_subnets`, `spot`) holds the EC2-specific
  defaults. Lightsail has no global section — its knobs are per-project. Omit
  all of it for a libvirt-only host.
- `[defaults]` (vcpus/memory_mib/disk_gb), `[ssh] pubkey_path`, `[base_image]
  url` — all defaulted.

### Per-project — `.devbox/config.toml` (at the project root, **not committed**)

```toml
# project = "<name>"    # instance label; default = the marker dir's basename
# provider = "libvirt"  # default provider for a bare `create`
# user, vcpus, memory_mib, disk_gb  # override the global [defaults]
mods = ["rust"]          # project mods, appended after the base playbook

[env]                    # creds/vars into the box's shell env; overrides global per-key
GITHUB_TOKEN = "github_pat_..."   # how the box gets GitHub auth (repo-scoped PAT)

[repo]                   # how the sandbox gets code; omit entirely for none
mode = "clone"            # clone | push | none
dir = "<project>"         # created at ~/<dir> in the sandbox
url = "https://github.com/<org>/<repo>"   # required iff mode == "clone"

# Host->box file sync (rsync; agent skills, git-excluded files). See "File sync".
[[sync]]                 # $HOME/~/absolute-anchored, resolved per side
src = "$HOME/.claude/skills"
include = ["jujutsu"]     # only these children of src (each mirrored)
[[repo.sync]]            # repo-relative: host = marker dir, guest = ~/<repo.dir>
src = ".claude/skills"

[ec2]                    # only for --provider ec2
instance_type = "m9g.xlarge"
architecture = "arm64"    # x86_64 | arm64

[lightsail]              # only for --provider aws; ALL keys optional (the whole
                         #   section can be omitted — `provider = "aws"` suffices)
# bundle = "xlarge_3_0"          # default: xlarge_3_0 = 16 GB RAM / 4 vCPU /
                                 #   320 GB SSD (~$84/mo). No `architecture`
                                 #   key: Lightsail bundles are x86-only.
# blueprint = "ubuntu_24_04"     # the OS image; default = newest Ubuntu LTS
                                 #   blueprint (older than the 26.04 userland
                                 #   libvirt/ec2 boot)
# availability_zone = "us-east-1b"   # default: <[aws].region> + "a"
```

- **Lightsail (`--provider aws`) specifics:** a bundle fixes CPU/RAM/disk
  together, so `disk_gb` is **ignored**, and bundles are **x86-only** (no ARM
  tier, hence no `architecture` key); the public IPv4 is **dynamic** (no
  static IP — `devbox ip`/`ssh` re-resolve it, so it may change across
  `stop`/`up`); **port 22 is open by default** (no security groups); and
  `--spot`/`--on-demand` are EC2-only and do nothing here.

- `name` is **not** a field (it errors — an instance is no longer a project).
- Exclude the dir locally (never `.gitignore`):
  `echo '.devbox/' >> .git/info/exclude`.
- **Repo modes:** `clone` (existing remote; needs `dir` + `url`), `push`
  (brand-new project, no remote yet; `dir` only — seed it after create, below),
  `none` (scratch VM; omit `[repo]`).
- **`[env]` creds:** injected into the box as a 0600 `~/.config/devbox/env`
  sourced by the shells (and exported into provisioning, so a private clone sees
  `GITHUB_TOKEN`). Written by `devbox sync --envvars` (auto-run on `create`).
  Values are plaintext in git-excluded `.devbox/config.toml` — accepted for
  scoped creds; don't put them in a committed file.
- **File sync:** `devbox sync --files` rsyncs host-local, non-VCS files onto the
  box (auto-run at the end of `create`). Use it for host-only files a mod can't
  reach (a mod runs box-side); skills in the repo arrive via `[repo]`, and
  GitHub-published skills belong in a mod. `dst` defaults to `src`; `$HOME`/`~`
  resolve per side; `include` narrows to named children (each its own
  `--delete` mirror unit, so it won't clobber box-installed skills outside the
  list). Host wins — box-side edits to a synced path are overwritten next sync.

### Mods (provisioning)

The base playbook (`jj → github-auth → dotfiles → vim → claude-code →
repo-bootstrap`) is built in. Add a project mod only for extra tooling
(`Cargo.toml` → a rust mod, `package.json` → a node mod):

1. List it: `mods = ["rust"]` in `.devbox/config.toml`.
2. Write `.devbox/mods/rust.sh` — self-contained and idempotent, taking context
   from the **environment** (`DEVBOX_USER`, `DEVBOX_JJ_NAME`, `DEVBOX_REPO_DIR`,
   …), not `%TOKEN%`s:

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   sudo apt-get install -y build-essential clang pkg-config libssl-dev  # dev user has NOPASSWD sudo
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   . "$HOME/.cargo/env"
   # ... cargo tools, `claude plugin install rust-analyzer-lsp`, etc.
   ```

Resolution is **project > global (`~/.config/devbox/mods`) > built-in** by name.
Every mod must be idempotent (it runs on `create` *and* every `devbox sync
--mods`): overwrite unconditionally or guard on "already done". A mod is the
*only* project-level extension point: devbox's first-boot layer is one fixed
init script it ships, and the old `.devbox/cloud-config.yaml` fragment no longer
exists.

A mod that needs another mod to run first declares it in a header comment:
`# requires: jj github-auth` (names space/comma-separated; **only** dep names —
no trailing inline comment). devbox validates this statically at playbook
resolution (before boot) and hard-errors if a required mod is missing or ordered
after the mod needing it — it doesn't reorder, just enforces order.

**What takes effect how:** provisioning runs entirely over SSH. A mod change (or
the `mods` list) reconverges a live box via `devbox sync --mods` — no rebuild. A
first-boot change (devbox's `init-script.sh`: base packages, the login user)
needs `devbox destroy` + `create`, since user-data runs only on first boot.

## Setting up & syncing

`devbox create` blocks: it boots the box, runs the mods over SSH, then pushes any
`[[sync]]` files — so when it returns the box is fully provisioned.

```bash
devbox create        # boot + provision (mods) + sync files; blocks until ready
devbox ssh
```

**Fetch the agent's work** (any provider): in the sandbox `jj bookmark create
wip -r @`; on the host `devbox jj fetch` then `jj new wip@devbox-<name>`.

**Seed a `mode = "push"` project** (once, after create):

```bash
devbox host                                        # prints the alias, devbox-<name>
jj git remote add devbox-<name> ssh://devbox-<name>/home/<user>/<dir>
jj bookmark set main -r @ --allow-backwards        # bookmark MUST be "main"
jj git push --remote devbox-<name> -b main
```

The bookmark must be `main` (matches the sandbox's `initial-branch`;
`receive.denyCurrentBranch=updateInstead` only updates the working tree for the
checked-out branch, and refuses a dirty tree — don't force it). See README's
"Exchanging commits with the sandbox" for the full model.
