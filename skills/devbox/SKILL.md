---
name: devbox
description: Explains devbox, a personal libvirt dev-sandbox VM manager, and walks through initializing a new project to use it — writing .devbox/config.toml and an optional .devbox/cloud-config.yaml, choosing a repo bootstrap mode (clone an existing remote vs. push-seed a brand-new project with no remote yet), and wiring up jj-sync. Use when the user asks to "set up devbox for this project", "create a devbox", "add a dev sandbox VM", or references devbox subcommands (devbox create/up/ssh/host/etc.) for a project that doesn't have one configured yet.
allowed-tools: Bash(devbox *), Bash(jj *), Bash(git *), Bash(gh *), Read, Write, Edit, AskUserQuestion
---

## 1. What devbox is

`devbox` is a global, multi-project dev-sandbox VM manager (libvirt +
cloud-init). One shared base VM image and one common cloud-config template
(dotfiles, jj install + identity, Claude Code + `github`/`linear` plugins,
base packages) are owned by devbox itself; each project supplies only what's
*different* — extra packages, a language toolchain, project-specific
scripts — via an optional `.devbox/cloud-config.yaml`, merged on top at
`devbox create` time. Full reference: `devbox`'s own `README.md` (same repo
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
  jj git push --remote sandbox --allow-new -b main
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
name = "<project>-dev"      # libvirt domain name
# user, vcpus, memory_mib, disk_gb optionally override the global [defaults]

[repo]                       # omit entirely for mode = "none"
mode = "clone"                # "clone" | "push"
dir = "<project>"             # created at ~/<dir> in the sandbox
url = "https://github.com/<org>/<repo>"   # required iff mode == "clone"
```

## 5. Decide whether `.devbox/cloud-config.yaml` is needed

Only if the project needs something beyond what the common template already
provides (dotfiles, jj, Claude Code + `github`/`linear` plugins, base
packages: zsh/vim/python3/curl/git/git-lfs/ca-certificates). Look at the
project's own tooling manifest to decide what to add — e.g. `Cargo.toml` →
a rustup-based toolchain install step; `package.json` → a bun/node install
step. If the project needs nothing extra, skip this file entirely (the
common template alone is a complete, valid cloud-config).

Worked example (`binius-zk/dev-sandbox`'s `.devbox/cloud-config.yaml`,
which adds a Rust toolchain + cargo tools on top of the common template):

```yaml
packages:
  - build-essential
  - clang
  - pkg-config
  - libssl-dev

write_files:
  - path: /usr/local/bin/provision-project.sh
    owner: root:root
    permissions: '0755'
    content: |
      #!/usr/bin/env bash
      set -euxo pipefail
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
      . "$HOME/.cargo/env"
      # ... project-specific toolchain setup ...

runcmd:
  - [ runuser, -l, "%DEVBOX_USER%", -c,
      "bash /usr/local/bin/provision-project.sh 2>&1 | tee /home/%DEVBOX_USER%/provision-project.log" ]
```

Merge rule to keep in mind while writing this file: `packages`,
`write_files`, `runcmd` (and `bootcmd`/`users`/`groups`) **concatenate**
onto the common template's list, common-first — don't redeclare `users:` or
a repo-clone step yourself, those are owned centrally by the common
template and driven by `[repo]` above. Every other top-level key (e.g.
`hostname`) is a whole-value override if you set it; if you don't, devbox
defaults `hostname` to the project's `name`.

`%DEVBOX_USER%`, `%DEVBOX_SSH_PUBKEY%`, `%DEVBOX_JJ_NAME%`,
`%DEVBOX_JJ_EMAIL%`, `%DEVBOX_REPO_MODE%`, `%DEVBOX_REPO_DIR%`,
`%DEVBOX_REPO_URL%` are available as literal `%TOKEN%` substitutions in this
file, same as the common template.

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

Remember: changing `.devbox/cloud-config.yaml` (or devbox's own common
template) only takes effect via `devbox destroy` + `devbox create`, never
`devbox up` — the cloud-init seed is only read on first boot.

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
