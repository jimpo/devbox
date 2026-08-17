---
name: devbox
description: devbox is a dev-sandbox VM manager for local libvirt VMs and AWS EC2 / Lightsail instances, provisioned by composable idempotent "mods". Several projects can share one VM, each in its own unprivileged account. Covers what it is, its command set, and how to configure a project (`.devbox/config.toml`: box, repo bootstrap, mods) plus the global `~/.config/devbox/config.toml`. Use when asked to set up devbox for a project, create/manage a dev sandbox VM, or when devbox subcommands (box create/create/ssh/sync/jj fetch/…) come up.
allowed-tools: Bash(devbox *), Bash(jj *), Bash(git *), Bash(gh *), Read, Write, Edit, AskUserQuestion
---

# devbox

A dev-sandbox VM manager for three providers — **libvirt** (local
VMs), **ec2** (AWS EC2), and **aws** (AWS Lightsail — flat-rate bundles, no
security groups to configure). All boxes share one cached base image, and every
project gets the same base
playbook of idempotent provisioning **mods** (jj, dotfiles, Claude Code, GitHub
auth, repo bootstrap) that devbox owns, adding only what's different
via `.devbox/config.toml`. Full reference: `README.md` in this repo.

**A box is not a project.** A *box* is a VM: provider, resources, image, and one
privileged account. It is created on its own (`devbox box create dev [flags]`)
and configured **only by command-line flags — there is no box config file**. A
*project* is a directory with a `.devbox/config.toml`, and it **attaches** to a
box (`devbox create`), getting its own unprivileged account there. Several
projects can share one box: the VM is the security boundary, the accounts are
organization.

**Accounts.** One privileged user per box (`--user`, default host `$USER`), which
provisioning runs as, plus one unprivileged account per attached project
(`agent_user`, default = the project name) with **no sudo**, under which that
project's AI agents, checkout and the `[env]` creds they need live. `devbox ssh`
lands on the project's account; `devbox box ssh <box>` is the privileged shell.
This contains system integrity, not
credentials — an agent still has network egress and still holds the tokens it
owns (`owner = "admin"` keeps one out of its reach).

## Commands

Box commands take the box's name. Project commands take none — `-C PATH` (project
dir) and `--box NAME` go *before* the subcommand.

| Command | Behavior |
|---|---|
| `devbox box create NAME [flags]` | Boot + provision a box. Flags (all optional): `--provider ec2\|aws --user --vcpus --memory --disk --instance-type --arch --ami --spot/--on-demand --bundle --blueprint --zone`. Idempotent; prints the canonical name to put in a project's `box =`. |
| `devbox box list [--attached]` | All devbox-managed boxes (libvirt on this host + EC2/Lightsail account-wide): name, provider, state, resources; `--attached` also shows which projects are on each. `devbox list` is the same command. |
| `devbox box up\|stop\|status\|destroy NAME` | Start / graceful-shutdown / cloud-init + the box's provision log / tear down (destroy deletes the disk, and names the attached projects first). |
| `devbox box ssh NAME` | SSH in as the privileged user — the box's only sudo shell. |
| `devbox box sync --mods[=a,b]` | Reconverge the box's own playbook (system state + the privileged account). |
| `devbox create` | Ensure the project's box exists, then **attach**: create its account, inject creds, run its mods, push files. Idempotent — re-running reconverges. `--adopt` claims an account devbox has no record of. |
| `devbox ip` / `ssh [-- args]` / `status` | Print IP / SSH in as this project's account / cloud-init progress + this project's provision log. |
| `devbox jj fetch` | jj-fetch the account's bookmarked work into your local repo (refreshes the SSH alias + jj remote, runs `jj git fetch`). |
| `devbox sync --envvars` | (Re-)write the 0600 `[env]` files — scoped creds/vars (`GITHUB_TOKEN`, API keys) into the shell env of each entry's `owner` account. Full rewrite each run. |
| `devbox sync --mods[=a,b]` | Run this project's provisioning mods over SSH on a live box (reconverge, no rebuild). No list = whole playbook; else just those. |
| `devbox sync --files` | Push host-local non-VCS files (`[[sync]]`/`[[repo.sync]]`) onto the box via rsync (agent skills, git-excluded files). |
| `devbox host` | Refresh/print this project's SSH alias, used for `jj fetch`. |
| `devbox detach [--purge]` | Give up this project's account (its record, key and creds go; the home survives unless `--purge`). |
| `devbox destroy` | Tear down the whole box — refused if any other project is attached. |

Box names: libvirt boxes stay **bare-named**; cloud boxes get a canonical
`<provider>-<name>` (e.g. `ec2-dev`, `aws-dev`) — use that in a project's
`box =` and in later commands. The prefix is added automatically, so pass the
bare name to `box create`.

## Configuration

### Global — `~/.config/devbox/config.toml` (optional; everything has a default)

- `[jj] name`/`email` — sandbox identity. Falls back to the host's own `jj
  config get user.name`/`user.email`, so usually no setup. If `devbox create`
  dies for lack of it, ask the user rather than guessing.
- `[env]` — a map of env vars (creds/API keys) injected into every box's shell
  env. **`GITHUB_TOKEN` here is how the box gets GitHub auth** (private
  clones/pushes); `gh`/`git`/`jj git` read it. A project's `[env]` overrides
  per-key. Unset is fine for public repos. An entry is a bare string (the agent's
  env) or `KEY = { value = "...", owner = "admin" }`, which puts it in the
  privileged user's env instead — for a cred only a privileged mod needs.
- `[aws] region` (+ optional `profile`) — the shared AWS account creds; enables
  **both** the `ec2` and `aws` (Lightsail) providers. `[ec2]` (optional
  `default_security_groups`, `default_subnets`, `spot`) holds the EC2-specific
  defaults. Omit all of it for a libvirt-only host.
- `[defaults]` (vcpus/memory_mib/disk_gb — what `box create` falls back to),
  `[ssh] pubkey_path`, `[base_image] url` — all defaulted.

### Per-box — command-line flags only

There is **no box config file**: a box's resources are fixed at create and
readable from the provider afterwards, so a stored spec could only disagree with
the machine. Anything not passed falls back to the global `[defaults]`.

```bash
devbox box create dev --vcpus 8 --memory 32768 --disk 200
devbox box create dev --provider ec2 --instance-type m9g.xlarge --arch arm64 [--ami ... --spot]
devbox box create dev --provider aws --bundle xlarge_3_0 [--blueprint ubuntu_24_04 --zone us-east-1b]
```

- **Lightsail (`--provider aws`) specifics:** a bundle fixes CPU/RAM/disk
  together, so `--disk` is **ignored**, and bundles are **x86-only** (no ARM
  tier, hence no arch flag); the public IPv4 is **dynamic** (no
  static IP — `devbox ip`/`ssh` re-resolve it, so it may change across
  `stop`/`up`); **port 22 is open by default** (no security groups); and
  `--spot`/`--on-demand` are EC2-only and do nothing here.
- Recreating a destroyed box means retyping the flags. Accepted trade-off.

### Per-project — `.devbox/config.toml` (at the project root, **not committed**)

```toml
# project = "<name>"    # the project's name; default = the marker dir's basename
# box = "<name>"        # the box to attach to (canonical name, e.g. "ec2-dev");
#                       #   default = the project's own name, i.e. a box of its own
# agent_user = "<name>" # unprivileged account agents run as; default = project
mods = ["rust"]          # project mods, appended after the base playbook

[env]                    # creds/vars into the box's shell env; overrides global per-key
GITHUB_TOKEN = "github_pat_..."   # how the box gets GitHub auth (repo-scoped PAT)
# CLOUDFLARE_TOKEN = { value = "...", owner = "admin" }  # privileged user's env

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
```

- `provider`/`user`/`vcpus`/`memory_mib`/`disk_gb`/`[ec2]`/`[lightsail]` are
  **not** project fields (they error, naming the `box create` flag to use) — they
  describe a box, which may host several projects.
- `name` is **not** a field either (it errors — a box is not a project).
- Exclude the dir locally (never `.gitignore`):
  `echo '.devbox/' >> .git/info/exclude`.
- **Repo modes:** `clone` (existing remote; needs `dir` + `url`), `push`
  (brand-new project, no remote yet; `dir` only — seed it after create, below),
  `none` (scratch VM; omit `[repo]`).
- **`[env]` creds:** injected into the home of the account each entry's `owner`
  names — the **agent user** by default — as a 0600 `~/.config/devbox/env`
  sourced by that user's shells (and by that account's mods at provision time, so
  a private clone sees `GITHUB_TOKEN`). Written by `devbox sync --envvars`
  (auto-run on `create`).
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

There are two built-in playbooks: the **box** one (`dotfiles-packages → dotfiles
→ vim`), which runs once per box as root/the privileged user, and the **project**
one (`jj → github-auth → dotfiles → vim → neovim → claude-code →
cargo-binstall → repo-bootstrap`), which runs once per attachment in that
project's account. Add a
project mod only for extra tooling (`Cargo.toml` → a rust mod, `package.json` →
a node mod):

1. List it: `mods = ["rust"]` in `.devbox/config.toml`.
2. Write `.devbox/mods/rust.sh` — self-contained and idempotent, taking context
   from the **environment** (`DEVBOX_AGENT_USER`, `DEVBOX_JJ_NAME`,
   `DEVBOX_REPO_DIR`, …), not `%TOKEN%`s:

   ```bash
   #!/usr/bin/env bash
   #
   # /// mod
   # packages = ["clang", "libssl-dev"]
   # ///
   #
   set -euo pipefail
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   . "$HOME/.cargo/env"
   # ... cargo tools, `claude plugin install rust-analyzer-lsp`, etc.
   ```

**Metadata goes in a `# /// mod` block** of comments holding TOML, right after
the shebang (PEP 723's shape, so the mod stays a runnable, shellcheck-able
script). The keys are `requires`, `runas` and `packages`, each an array of
strings, all optional. devbox parses it host-side and hard-errors on an unknown
key, a bad value or a duplicate block before any boot/SSH. Don't use the old
`# packages: jq` header lines — a fenced block is the only form devbox reads now.

**A mod body has no sudo.** It runs as the project's unprivileged account, so
`sudo` in a mod is a bug. Declare apt packages with `packages` (the driver
installs them as the privileged user beforehand), and use `runas = ["root"]` for
the rare step that genuinely needs privilege. `runas` defaults to `["agent"]` and
also accepts `admin`; a mod naming more than one target runs once per target.

`runas` also decides **scope**: a mod that names `agent` runs only its agent
half in a project playbook (and everything else in the box playbook — that is how
`dotfiles` sets up the privileged account once per box and each account
separately). A mod that names no agent target is pure system work and runs whole
in whichever playbook lists it — so a project's `runas = ["root"]` mod (docker, a
driver) lands on the shared box when it attaches. Prefer `$HOME` installs:
anything system-wide is shared with every other project on that box, and devbox
prints a warning naming the mod.

Resolution is **project > global (`~/.config/devbox/mods`) > built-in** by name.
Every mod must be idempotent (it runs on `create` *and* every `devbox sync
--mods`): overwrite unconditionally or guard on "already done". A mod is the
*only* project-level extension point: devbox's first-boot layer is one fixed
init script it ships, and the old `.devbox/cloud-config.yaml` fragment no longer
exists.

A mod that needs another mod to run first declares it in its metadata block:
`requires = ["jj", "github-auth"]`. devbox validates this statically at playbook
resolution (before boot) and hard-errors if a required mod is missing or ordered
after the mod needing it — it doesn't reorder, just enforces order. The box
playbook is indexed first, so a project mod may depend on a box mod.

**What takes effect how:** provisioning runs entirely over SSH. A mod change (or
the `mods` list) reconverges a live box via `devbox sync --mods` — no rebuild. A
first-boot change (devbox's `init-script.sh`: base packages, the privileged user)
needs `devbox box destroy` + `box create`, since user-data runs only on first
boot. Project accounts are *not* first-boot — the driver creates them, so
attaching a project (or changing `agent_user`) reconverges instead.

## Setting up & syncing

`devbox create` blocks: it ensures the box, creates this project's account, runs
its mods over SSH, then pushes any `[[sync]]` files — so when it returns the
project is fully provisioned.

```bash
devbox box create dev      # once per machine; omit if the project owns its box
# then, in the project (with `box = "dev"` set, or nothing at all):
devbox create              # attach + provision + sync files; blocks until ready
devbox ssh
```

**Fetch the agent's work** (any provider): in the sandbox `jj bookmark create
wip -r @`; on the host `devbox jj fetch` then `jj new wip@<alias>`, where the
alias is `devbox-<box>` when the project owns the box and
`devbox-<box>-<project>` on a shared one (`devbox host` prints it).

**Seed a `mode = "push"` project** (once, after create):

```bash
devbox host                                        # prints the alias
jj git remote add <alias> ssh://<alias>/home/<agent_user>/<dir>
jj bookmark set main -r @ --allow-backwards        # bookmark MUST be "main"
jj git push --remote <alias> -b main
```

The bookmark must be `main` (matches the sandbox's `initial-branch`;
`receive.denyCurrentBranch=updateInstead` only updates the working tree for the
checked-out branch, and refuses a dirty tree — don't force it). See README's
"Exchanging commits with the sandbox" for the full model.
