---
name: devbox
description: devbox is a multi-project dev-sandbox VM manager for local libvirt VMs and AWS EC2 instances, provisioned by composable idempotent "mods". Covers what it is, its command set, and how to configure a project (`.devbox/config.toml`: repo bootstrap, mods, AWS) plus the global `~/.config/devbox/config.toml`. Use when asked to set up devbox for a project, create/manage a dev sandbox VM, or when devbox subcommands (create/up/ssh/provision/sync/…) come up.
allowed-tools: Bash(devbox *), Bash(jj *), Bash(git *), Bash(gh *), Read, Write, Edit, AskUserQuestion
---

# devbox

A multi-project dev-sandbox VM manager for two providers — **libvirt** (local
VMs) and **AWS** (EC2). All projects share one cached base image and a base
playbook of idempotent provisioning **mods** (jj, dotfiles, Claude Code, GitHub
auth, repo bootstrap) that devbox owns; each project adds only what's different
via `.devbox/config.toml`. Full reference: `README.md` in this repo.

## Commands

`-C PATH` (project dir) and `--name NAME` (target a specific instance) go
*before* the subcommand.

| Command | Behavior |
|---|---|
| `devbox create` | Boot + provision the project's default libvirt VM, or a named cloud instance with `--provider aws --name <n>`. |
| `devbox list` | All devbox-managed instances (libvirt on this host + AWS account-wide): name, provider, state, resources. |
| `devbox up` / `stop` / `destroy` | Start / graceful-shutdown / tear down (destroy also deletes the disk). |
| `devbox ip` / `ssh [-- args]` / `status` | Print IP / SSH in / show cloud-init progress + `~/provision.log`. |
| `devbox provision [mods…]` | Re-run provisioning mods over SSH on a live box (reconverge, no rebuild). No args = whole playbook; names = just those. |
| `devbox sync` | jj-fetch the instance's bookmarked work into your local repo (refreshes the SSH alias + `devbox-<name>` jj remote, runs `jj git fetch`). |
| `devbox host` | Refresh/print the `devbox-<name>` SSH alias used for jj-sync. |

Instances: the libvirt default is **bare-named = the project**; cloud instances
take an explicit `--name` and get a canonical `<provider>-<name>` (e.g.
`aws-foo`) — use that in later commands. `--provider` is create-only (the
provider is derived from the name).

## Configuration

### Global — `~/.config/devbox/config.toml` (optional; everything has a default)

- `[jj] name`/`email` — sandbox identity. Falls back to the host's own `jj
  config get user.name`/`user.email`, so usually no setup. If `devbox create`
  dies for lack of it, ask the user rather than guessing.
- `[github] token` — a fine-grained PAT, only for private repos or pushing to
  GitHub. A project's `[github].token` overrides it. Unset is fine for public
  repos (devbox just prints a note).
- `[aws] region` (+ optional `profile`, `default_security_groups`,
  `default_subnets`, `spot`) — enables the AWS provider. Omit for a
  libvirt-only host.
- `[defaults]` (vcpus/memory_mib/disk_gb), `[ssh] pubkey_path`, `[base_image]
  url` — all defaulted.

### Per-project — `.devbox/config.toml` (at the project root, **not committed**)

```toml
# project = "<name>"    # instance label; default = the marker dir's basename
# provider = "libvirt"  # default provider for a bare `create`
# user, vcpus, memory_mib, disk_gb  # override the global [defaults]
mods = ["rust"]          # project mods, appended after the base playbook

[repo]                   # how the sandbox gets code; omit entirely for none
mode = "clone"            # clone | push | none
dir = "<project>"         # created at ~/<dir> in the sandbox
url = "https://github.com/<org>/<repo>"   # required iff mode == "clone"

[aws]                    # only for --provider aws
instance_type = "m9g.xlarge"
architecture = "arm64"    # x86_64 | arm64
```

- `name` is **not** a field (it errors — an instance is no longer a project).
- Exclude the dir locally (never `.gitignore`):
  `echo '.devbox/' >> .git/info/exclude`.
- **Repo modes:** `clone` (existing remote; needs `dir` + `url`), `push`
  (brand-new project, no remote yet; `dir` only — seed it after create, below),
  `none` (scratch VM; omit `[repo]`).

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
Every mod must be idempotent (it runs on first boot *and* every `devbox
provision`): overwrite unconditionally or guard on "already done". A `.devbox/
cloud-config.yaml` is now rarely needed (only genuine cloud-init concerns);
prefer a mod, since a cloud-config `runcmd` is first-boot-only.

**What takes effect how:** a mod change (or the `mods` list) reconverges live via
`devbox provision`. A cloud-config-layer change (`packages`/`users`/the common
template) needs `devbox destroy` + `create` — the cloud-init seed is read only on
first boot.

## Setting up & syncing

```bash
devbox create && devbox status   # boot + provision; wait for "status: done"
devbox ssh
```

**Fetch the agent's work** (any provider): in the sandbox `jj bookmark create
wip -r @`; on the host `devbox sync` then `jj new wip@devbox-<name>`.

**Seed a `mode = "push"` project** (once, after create):

```bash
devbox host                                        # prints the alias, devbox-<name>
jj git remote add devbox-<name> ssh://devbox-<name>/home/<user>/<dir>
jj bookmark set main -r @ --allow-backwards        # bookmark MUST be "main"
jj git push --remote devbox-<name> -b main
```

The bookmark must be `main` (matches the sandbox's `initial-branch`;
`receive.denyCurrentBranch=updateInstead` only updates the working tree for the
checked-out branch, and refuses a dirty tree — don't force it). See README's "jj
sync" for the full model.
