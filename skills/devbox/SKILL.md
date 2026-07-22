---
name: devbox
description: Explains devbox, a personal libvirt dev-sandbox VM manager, and walks through initializing a new project to use it — writing .devbox/config.toml and an optional .devbox/cloud-config.yaml, choosing a repo bootstrap mode (clone an existing remote vs. push-seed a brand-new project with no remote yet), and wiring up jj-sync. Use when the user asks to "set up devbox for this project", "create a devbox", "add a dev sandbox VM", or references devbox subcommands (devbox create/up/ssh/host/etc.) for a project that doesn't have one configured yet.
allowed-tools: Bash(devbox *), Bash(jj *), Bash(git *), Bash(gh *), Read, Write, Edit, AskUserQuestion
---

## 1. What devbox is

`devbox` is a global, multi-project dev-sandbox VM manager (libvirt +
cloud-init). One shared base VM image and a base playbook of idempotent
provisioning "mods" (dotfiles, jj install + identity, Claude Code +
`github`/`linear` plugins, GitHub auth, repo bootstrap) are owned by devbox
itself; each project appends only what's *different* — a language toolchain,
project tooling — as its own mods via a `mods = [...]` list in
`.devbox/config.toml`. Full reference: `devbox`'s own `README.md` (same repo
as this skill).

## 2. Check one-time host setup

`devbox create` needs a jj identity for the sandbox. It resolves this from
`~/.config/devbox/config.toml`'s `[jj]` table if set:

```toml
[jj]
name = "..."
email = "..."
```

...falling back to the host's own `jj config get user.name`/`user.email` if
`[jj]` isn't set (which is almost always already configured if the user
uses jj) — so this usually needs no action. It's only a real problem if
`devbox create` actually dies over it (no `[jj]` set *and* no `jj` identity
on the host either); in that case ask the user for their name/email rather
than guessing. Everything else in the global config (base image,
vcpus/memory/disk defaults, ssh pubkey path) has a sensible built-in
default and usually needs no setup either.

GitHub auth is separate and never required: `[github] token`, no host
fallback — deliberately a fine-grained PAT the user scopes themselves in
devbox's config, not whatever `gh auth login` session happens to exist on
the host. It only matters for private repos or pushing back to GitHub from
inside the sandbox — `devbox create` just prints a note and continues if
it's unset, which is fine for public repos. If the project needs one and
`[github].token` isn't set, ask the user to add it rather than pulling a
token from anywhere else.

## 3. Decide the repo bootstrap mode

Ask: does the project already have a git remote (something to `jj git
clone`)?

- **Yes** → `mode = "clone"`. Needs `dir` (directory name created at
  `~/<dir>` in the sandbox) and `url`.
- **No** — brand-new project, nothing pushed anywhere yet → `mode = "push"`.
  Needs `dir` only (`url` is not allowed in this mode — devbox will die if
  both are set). The sandbox creates an *empty* colocated jj+git repo ready
  to receive a push; after `devbox create`, seed it from the host:
  ```bash
  devbox host                                     # prints alias, e.g. devbox-<name>
  jj git remote add sandbox ssh://<alias>/home/<user>/<dir>
  jj bookmark set main -r @ --allow-backwards     # bookmark name MUST be "main"
  jj git push --remote sandbox -b main
  ```
  The bookmark name **must** be exactly `main` — the sandbox repo is
  initialized with `git init --initial-branch=main`, and
  `receive.denyCurrentBranch=updateInstead` (which is what makes the push
  actually populate the sandbox's working tree, not just update a ref) only
  fires when the pushed branch matches the one currently checked out. This
  also refuses the push outright if the sandbox-side working tree is dirty
  — a safety property, not a bug; if a push is rejected, check what's
  uncommitted in the sandbox rather than forcing it.
- **Neither** (no repo needed — a scratch/experiment VM) → omit `[repo]`
  entirely, or `mode = "none"`.

## 4. Write `.devbox/config.toml`

At the project's root, **not committed to git**:

```toml
# project = "<name>"        # label on this project's instances
#                           # (default: the marker directory's basename)
# user, vcpus, memory_mib, disk_gb optionally override the global [defaults]

[repo]                       # omit entirely for mode = "none"
mode = "clone"                # "clone" | "push"
dir = "<project>"             # created at ~/<dir> in the sandbox
url = "https://github.com/<org>/<repo>"   # required iff mode == "clone"
```

`name` is **not** a field (it errors) — an instance is no longer a project.
The libvirt default instance is bare-named = the project. Cloud instances take
an explicit `--name` at create, and devbox stores a canonical
`<provider>-<name>` (e.g. `aws-binius-arm`); reference that canonical name in
later commands (`devbox --name aws-binius-arm ssh`). The provider is derived
from the name, so `--provider` is create-only.

## 5. Decide whether the project needs a provisioning mod

Provisioning is a list of idempotent bash **mods**. The built-in base playbook
(`jj → github-auth → dotfiles → vim → claude-code → repo-bootstrap`) already
covers dotfiles, jj, Claude Code + `github`/`linear` plugins, GitHub auth, and
the `[repo]` bootstrap. Add a project mod only for what's beyond that — look at
the project's tooling manifest: `Cargo.toml` → a rust mod; `package.json` → a
bun/node mod. If nothing extra is needed, skip this step.

To add one:

1. List it in `.devbox/config.toml` (appended after the base playbook):
   ```toml
   mods = ["rust"]
   ```
2. Write `.devbox/mods/rust.sh` — a self-contained, idempotent script that
   `sudo apt-get install -y`s its own deps (the dev user has NOPASSWD sudo) and
   receives context via **environment variables** (`DEVBOX_USER`,
   `DEVBOX_JJ_NAME`, `DEVBOX_REPO_DIR`, …), NOT `%TOKEN%` substitution:

   ```bash
   #!/usr/bin/env bash
   # mod: rust — toolchain + cargo tooling. Idempotent (rustup/cargo re-runs are safe).
   set -euo pipefail
   sudo apt-get install -y build-essential clang pkg-config libssl-dev
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   . "$HOME/.cargo/env"
   # ... cargo tools, claude plugin install rust-analyzer-lsp, etc.
   ```

Resolution is **project > global > built-in** by name, so `.devbox/mods/<name>.sh`
shadows a built-in of the same name. `devbox provision rust` re-applies just
that mod on a live box (fast iteration); bare `devbox provision` runs the whole
playbook.

**Idempotency is required**: every mod runs on first boot AND on every
`devbox provision`, so each step must either overwrite unconditionally or guard
on "already done".

A `.devbox/cloud-config.yaml` is now rarely needed — only for genuine
cloud-init concerns (extra `packages`, a `write_files` entry). It still supports
`%DEVBOX_USER%`/`%DEVBOX_SSH_PUBKEY%`. List keys (`packages`, `write_files`,
`runcmd`, …) concatenate onto the common template's; don't redeclare `users:`.
Prefer a mod over a cloud-config `runcmd` — the runcmd is first-boot-only and
never reconverges.

## 6. Exclude `.devbox/` locally

Nothing under `.devbox/` should be committed — it's this machine's personal
VM config, not something to share with collaborators. If the project is a
git repo, append `.devbox/` to `.git/info/exclude` (**not** the tracked
`.gitignore`):

```bash
mkdir -p .git/info
grep -qxF '.devbox/' .git/info/exclude 2>/dev/null || echo '.devbox/' >> .git/info/exclude
```

If the project root isn't itself a git repo (e.g. a plain directory with a
git clone nested inside it), there's no exclude file needed at all — nothing
here is tracked either way.

## 7. Run it

```bash
devbox create   # download base image (cached), render + merge cloud-config, define + first-boot
devbox status   # watch cloud-init progress; wait for "status: done"
devbox ssh      # once provisioning finishes
```

Remember: changing a **mod** (or the `mods` list) reconverges on a live box
with `devbox provision` — no rebuild. But changing the **cloud-config layer**
(`.devbox/cloud-config.yaml` or devbox's common template — `packages`/`users`/
`BASH_ENV`) only takes effect via `devbox destroy` + `devbox create`, since the
cloud-init seed is read only on first boot.

## 8. jj-sync (both directions)

Once the VM is up, the sandbox is a plain SSH-reachable jj/git remote for
the host:

- **Fetch** (agent's work → host, always available): inside the sandbox,
  `jj bookmark create wip -r @` on anything that should become visible; on
  the host, `devbox host` then `jj git remote add sandbox
  ssh://<alias>/home/<user>/<dir>` (one-time), then `devbox host` + `jj git
  fetch --remote sandbox` + `jj new wip@sandbox` (ongoing — rerun `devbox
  host` each time since the sandbox's IP/host key can change across
  `stop`/`up` cycles).
- **Push** (host → sandbox, only relevant for `mode = "push"` projects): see
  step 3 above — same `sandbox` remote, bookmark name `main` instead of
  `wip`.
