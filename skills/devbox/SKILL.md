---
name: devbox
description: devbox is a dev-sandbox VM manager for local libvirt VMs and AWS EC2 / Lightsail instances, provisioned by composable idempotent "mods". Several workspaces can share one VM, each in its own unprivileged account. Covers what it is, its command set, and how to configure a workspace (`devbox.toml` / `.devbox/config.toml`: box, repos, mods) plus the global `~/.config/devbox/config.toml`. Use when asked to set up devbox for a workspace, create/manage a dev sandbox VM, or when devbox subcommands (box create/create/ssh/sync/jj fetch/…) come up.
allowed-tools: Bash(devbox *), Bash(jj *), Bash(git *), Bash(gh *), Read, Write, Edit, AskUserQuestion
---

# devbox

A dev-sandbox VM manager for three providers — **libvirt** (local
VMs), **ec2** (AWS EC2), and **aws** (AWS Lightsail — flat-rate bundles, no
security groups to configure). All boxes share one cached base image, and every
workspace gets the same base
playbook of idempotent provisioning **mods** (jj, dotfiles, Claude Code, GitHub
auth, repo bootstrap) that devbox owns, adding only what's different
via its marker file. Full reference: `README.md` in this repo.

**A box is not a workspace.** A *box* is a VM: provider, resources, image, and one
privileged account. It is created on its own (`devbox box create dev [flags]`)
and configured **only by command-line flags — there is no box config file**. A
*workspace* is a marked directory holding one or more repos, and it **attaches** to a
box (`devbox create`), getting its own unprivileged account there. Several
workspaces can share one box: the VM is the security boundary, the accounts are
organization.

**Accounts.** One privileged user per box (`--user`, default `admin`), which
provisioning runs as, plus one unprivileged account per attached workspace
(`agent_user`, default = the workspace name) with **no sudo**, under which that
workspace's AI agents, all its checkouts and the `[env]` creds they need live —
which is what makes several repos in one workspace share a set of git/jj creds. `devbox ssh`
lands on the workspace's account; `devbox box ssh <box>` is the privileged shell.
This contains system integrity, not
credentials — an agent still has network egress and still holds the tokens it
owns (`owner = "admin"` keeps one out of its reach).

## Commands

Box commands take the box's name. Workspace commands take none — `-C PATH` (workspace
dir) and `--box NAME` go *before* the subcommand.

| Command | Behavior |
|---|---|
| `devbox box create NAME [flags]` | Boot + provision a box. Flags (all optional): `--provider ec2\|aws --user --vcpus --memory --disk --instance-type --arch --ami --spot/--on-demand --bundle --blueprint --zone`. Idempotent; prints the canonical name to put in a workspace's `box =`. |
| `devbox box list [--attached]` | All devbox-managed boxes (libvirt on this host + EC2/Lightsail account-wide): name, provider, state, resources; `--attached` also shows which workspaces are on each. `devbox list` is the same command. |
| `devbox box up\|stop\|status\|destroy NAME` | Start / graceful-shutdown / cloud-init + the box's provision log / tear down (destroy deletes the disk, and names the attached workspaces first). |
| `devbox box ssh NAME` | SSH in as the privileged user — the box's only sudo shell. |
| `devbox box sync --mods` | Reconverge the box's own playbook (system state + the privileged account). |
| `devbox create` | Ensure the workspace's box exists, then **attach**: create its account, inject creds, run its mods, push files. Idempotent — re-running reconverges. `--adopt` claims an account devbox has no record of. |
| `devbox ip` / `ssh [-- args]` / `status` | Print IP / SSH in as this workspace's account / cloud-init progress + this workspace's provision log. |
| `devbox jj fetch [REPO]` | jj-fetch one checkout's bookmarked work into your local repo (refreshes the SSH alias + jj remote, runs `jj git fetch`). Picks the repo you name, else the one the cwd is inside, else the workspace's only one. |
| `devbox sync --envvars` | (Re-)write the 0600 `[env]` files — scoped creds/vars (`GITHUB_TOKEN`, API keys) into the shell env of each entry's `owner` account. Full rewrite each run. |
| `devbox sync --mods` | Run this workspace's provisioning mods over SSH on a live box (reconverge, no rebuild). Always the whole playbook. |
| `devbox sync --files` | Push host-local non-VCS files (`[[sync]]`/`[[repos.sync]]`) onto the box via rsync (agent skills, git-excluded files). |
| `devbox host` | Refresh/print this workspace's SSH alias, used for `jj fetch`. |
| `devbox herdr [-- args]` | Launch a herdr session on the box as this workspace's account (`herdr --remote <alias>`). Replaces `herdr --remote $(devbox host)`; trailing args pass through. Needs herdr on the **host**. |
| `devbox detach [--purge]` | Give up this workspace's account (its record, key and creds go; the home survives unless `--purge`). |
| `devbox destroy` | Tear down the whole box — refused if any other workspace is attached. |

Box names: libvirt boxes stay **bare-named**; cloud boxes get a canonical
`<provider>-<name>` (e.g. `ec2-dev`, `aws-dev`) — use that in a workspace's
`box =` and in later commands. The prefix is added automatically, so pass the
bare name to `box create`.

## Configuration

### Global — `~/.config/devbox/config.toml` (optional; everything has a default)

- `[jj] name`/`email` — sandbox identity. Falls back to the host's own `jj
  config get user.name`/`user.email`, so usually no setup. If `devbox create`
  dies for lack of it, ask the user rather than guessing.
- `[env]` — a map of env vars (creds/API keys) injected into every box's shell
  env. **`GITHUB_TOKEN` here is how the box gets GitHub auth** (private
  clones/pushes); `gh`/`git`/`jj git` read it. A workspace's `[env]` overrides
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

### Per-workspace — the marker file (**not committed**)

Two spellings, and which one you use says what shape the directory has:

- **`<repo>/.devbox/config.toml`** — the workspace IS one repo; the marker hides
  in its tree. Mods go in `.devbox/mods/`.
- **`<root>/devbox.toml`** — the workspace HOLDS repos, as children of the root.
  Nothing to hide from, so the marker is visible. Mods go in `<root>/mods/`.

devbox walks up for either, nearest level first; a directory with both errors.

```toml
# workspace = "<name>"    # the workspace's name; default = the marker dir's basename
# box = "<name>"        # the box to attach to (canonical name, e.g. "ec2-dev");
#                       #   default = the workspace's own name, i.e. a box of its own
# agent_user = "<name>" # unprivileged account agents run as; default = workspace
mods = ["rust"]          # workspace mods, appended after the base playbook

[env]                    # creds/vars into the box's shell env; overrides global per-key
GITHUB_TOKEN = "github_pat_..."   # how the box gets GitHub auth (repo-scoped PAT)
# CLOUDFLARE_TOKEN = { value = "...", owner = "admin" }  # privileged user's env

# One [[repos]] block per checkout; omit them all for no repo bootstrap. They all
# land in the one account, so they share its git/jj credentials.
[[repos]]
name = "<repo>"          # addresses it, and defaults both `host` and `dir`.
                         #   Omit ONLY under a `.devbox/` marker: it then means
                         #   "the workspace root is the repo".
mode = "clone"           # clone | push
url = "https://github.com/<org>/<repo>"   # required iff mode == "clone"
# host = "<repo>"        # host path relative to the workspace root (default: name)
# dir  = "<repo>"        # lands at ~/<dir> on the box (default: name)

# Host->box file sync (rsync; agent skills, git-excluded files). See "File sync".
[[sync]]                 # $HOME/~/absolute-anchored, resolved per side
src = "$HOME/.claude/skills"
include = ["jujutsu"]     # only these children of src (each mirrored)
[[sync]]                 # bare-relative: workspace root -> the agent's home.
src = "shared-notes"      #   `devbox.toml` markers only (see below)
[[repos.sync]]           # nested under a [[repos]] entry: that checkout, both sides
src = ".claude/skills"
```

- `provider`/`user`/`vcpus`/`memory_mib`/`disk_gb`/`[ec2]`/`[lightsail]` are
  **not** workspace fields (they error, naming the `box create` flag to use) — they
  describe a box, which may host several workspaces.
- `name` is **not** a field either (it errors — a box is not a workspace). Nor is
  `project` (now `workspace`) or `[repo]` (now `[[repos]]`); both error saying so.
- For the `.devbox/` shape, exclude it locally (never `.gitignore`):
  `echo '.devbox/' >> .git/info/exclude`. A `devbox.toml` sits outside every repo
  and needs no excluding.
- **Repo modes:** `clone` (existing remote; needs `url`), `push` (no remote yet —
  seed it after create, below). There is no `none`: a workspace with nothing to
  check out lists no repos.
- **Workspace-anchored `[[sync]]`** (a bare-relative `src`) works only under a
  `devbox.toml` marker, where the root and the agent's home carry the same child
  names. Under `.devbox/` the root is a repo and the two differ, so devbox
  refuses it and points at `[[repos.sync]]`.
- **`[env]` creds:** injected into the home of the account each entry's `owner`
  names — the **agent user** by default — as a 0600 `~/.config/devbox/env`
  sourced by that user's shells (and by that account's mods at provision time, so
  a private clone sees `GITHUB_TOKEN`). Written by `devbox sync --envvars`
  (auto-run on `create`).
  Values are plaintext in the git-excluded marker file — accepted for
  scoped creds; don't put them in a committed file.
- **File sync:** `devbox sync --files` rsyncs host-local, non-VCS files onto the
  box (auto-run at the end of `create`). Use it for host-only files a mod can't
  reach (a mod runs box-side); skills in a tracked repo arrive via `[[repos]]`, and
  GitHub-published skills belong in a mod. `dst` defaults to `src`; `$HOME`/`~`
  resolve per side; `include` narrows to named children (each its own
  `--delete` mirror unit, so it won't clobber box-installed skills outside the
  list). Host wins — box-side edits to a synced path are overwritten next sync.

### Mods (provisioning)

There are two built-in playbooks: the **box** one (`box-packages → nix`),
which runs once per box as root, and the **workspace** one (`home-manager →
github-auth → jj → dotfiles → neovim → claude-code → cargo-binstall →
repo-bootstrap`), which runs once per attachment in that
workspace's account. A workspace names anything else it needs in `mods`, which
built-in opt-ins answer to as well as its own: `mods = ["nodejs"]` gets the
Node.js LTS with npm from the generation. Add a mod of your own for tooling no
built-in covers (`Cargo.toml` → a rust mod):

1. List it: `mods = ["rust"]` in the marker file.
2. Write `<mods dir>/rust.sh` (`.devbox/mods/` or `mods/`, matching the marker)
   — self-contained and idempotent, taking context from the **environment**
   (`DEVBOX_AGENT_USER`, `DEVBOX_JJ_NAME`, `DEVBOX_WORKSPACE`, …), not
   `%TOKEN%`s. The checkouts are the one thing too long for the environment:
   they arrive as `$DEVBOX_PAYLOAD_DIR/repos`, one
   `<name>\t<mode>\t<dir>\t<url>` record each (what `repo-bootstrap` walks):

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

**A mod body has no sudo.** It runs as the workspace's unprivileged account, so
`sudo` in a mod is a bug. Declare apt packages with `packages` (the driver
installs them as the privileged user beforehand), and use `runas = ["root"]` for
the rare step that genuinely needs privilege. `runas` defaults to `["agent"]` and
also accepts `admin`; a mod naming more than one target runs once per target.

`runas` also decides **scope**: a mod that names `agent` runs only its agent
half in a workspace playbook (and everything else in the box playbook — useful
for a mod that sets up the privileged account once per box while each
workspace's account gets its own agent half, as `dotfiles` used to before it
became agent-only). A mod that names no agent target is pure system work and runs whole
in whichever playbook lists it — so a workspace's `runas = ["root"]` mod (docker, a
driver) lands on the shared box when it attaches. Prefer `$HOME` installs:
anything system-wide is shared with every other workspace on that box, and devbox
prints a warning naming the mod.

Resolution is **workspace > global (`~/.config/devbox/mods`) > built-in** by name.
A nix module (a converted mod, or one that never was a script - see below)
resolves by the same precedence out of the **same** directories, as a
`<name>.nix` beside the `<name>.sh` scripts: `<workspace mods dir>/`, then
`~/.config/devbox/mods/`, then devbox's own built-in `devbox/mods/`. One
directory per level, holding both halves; a `<name>.sh` anywhere still wins
over any module of that name. `devbox nix check` evaluates the modules on the
host - it assembles a payload flake, since `devbox/nix/` is a skeleton that
cannot be evaluated as it sits - and is the only way to check one without a
box; run it on any `.nix` change.
Every mod must be idempotent (it runs on `create` *and* every `devbox sync
--mods`): overwrite unconditionally or guard on "already done". A mod is the
*only* workspace-level extension point: devbox's first-boot layer is one fixed
init script it ships, and the old `.devbox/cloud-config.yaml` fragment no longer
exists.

A mod that needs another mod to run first declares it in its metadata block:
`requires = ["jj", "github-auth"]`. devbox validates this statically at playbook
resolution (before boot) and hard-errors if a required mod is missing or ordered
after the mod needing it — it doesn't reorder, just enforces order. The box
playbook is indexed first, so a workspace mod may depend on a box mod.

**What takes effect how:** provisioning runs entirely over SSH. A mod change (or
the `mods` list) reconverges a live box via `devbox sync --mods` — no rebuild. A
first-boot change (devbox's `init-script.sh`: base packages, the privileged user)
needs `devbox box destroy` + `box create`, since user-data runs only on first
boot. Workspace accounts are *not* first-boot — the driver creates them, so
attaching a workspace (or changing `agent_user`) reconverges instead.

## Setting up & syncing

`devbox create` blocks: it ensures the box, creates this workspace's account, runs
its mods over SSH, then pushes any `[[sync]]` files — so when it returns the
workspace is fully provisioned.

```bash
devbox box create dev      # once per machine; omit if the workspace owns its box
# then, in the workspace (with `box = "dev"` set, or nothing at all):
devbox create              # attach + provision + sync files; blocks until ready
devbox ssh
```

**Fetch the agent's work** (any provider): in the sandbox `jj bookmark create
wip -r @`; on the host `devbox jj fetch` then `jj new wip@<alias>`, where the
alias is `devbox-<box>` when the workspace owns the box and
`devbox-<box>-<workspace>` on a shared one (`devbox host` prints it). One alias
covers every checkout in the workspace — the alias is the account, each repo a
path inside it — so `devbox jj fetch` takes the repo you name, else the one your
cwd is inside, else the workspace's only one.

**Seed a `mode = "push"` repo** (once, after create):

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
