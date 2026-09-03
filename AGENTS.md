# AGENTS.md

## Dev loop

```bash
uv sync                          # create .venv with dev deps (pytest)
uv run pytest                    # run the test suite
uv run devbox nix check          # evaluate the home-manager modules (needs `nix`
                                  # on PATH; --build also realises the closure)
uv tool install --editable .     # point the live `devbox` command at this checkout
                                  # (editable install - no reinstall needed after further edits)
```

`pytest` never evaluates nix. Any change under `devbox/nix/` or to a
`devbox/mods/*.nix` needs `devbox nix check` too - see Testing scope.

## Testing scope

`tests/` exercises pure logic and provider paths reachable without real infra:
global/workspace config loading + merging (`GlobalConfig.load`,
`resolve_workspace`, `resolve_instance`, `parse_instance_name`), the
box/workspace split (`resolve_box_spec` from argv, the default disk size a
shared `/nix` store has to fit inside, the rejection of box-level
keys in a workspace's config, `box` defaulting, `agent_user` defaulting +
username/reserved-name validation), the two marker spellings (`find_workspace`:
walk-up for either, nearest level wins, both-at-once refused, and the
`root_is_repo`/mods-dir each implies), the `[[repos]]` list (name-supplied host
path + guest dir, the nameless "the root is the repo" entry and its refusal
under `devbox.toml`, distinct names/dirs, the mods-dir collision, clone/push
validation, and the hard errors on the old `[repo]`/`project` keys), the
first-boot
init-script render (token substitution into the shipped `init-script.sh`, now
box-scoped), the
modular-provisioning logic (mod resolution
precedence — now keyed on the marker's own mods directory — the `# /// mod`
metadata block — `requires`/`runas`/`packages`, plus
its rejection of unknown keys, bad values, duplicate and mislabeled fences — the
two playbooks and `scoped_runas`'s narrowing of each mod's targets (`dotfiles`
now agent-only, so the box playbook is apt-only — just `dotfiles-packages` and
`nix` — and `nix`'s box
scope + the `trusted-users` line that names the privileged account and never an
agent, and `home-manager`'s agent scope + its `requires = ["nix"]` across that
same boundary, and the vim split — `vim` resolving to the flake's module with no
`vim.sh` anywhere, and no admin-side implementation left at all: the
privileged account keeps stock, unconfigured apt vim), manifest
rendering, `render_repos` + the `repos` payload file, env-file quoting,
the home-manager flake (`render_workspace_nix`: the rendered attribute set, nix
string escaping and bare-vs-quoted attribute names, a `<name>.enable` flag per
shipped module, and `nix_payload_files` assembling the `NIX_DIR` skeleton and
one `modules/<name>.nix` per resolved module with an attachment but nothing
with a box; that `NIX_DIR` itself holds only the three skeleton files, no
`modules/` and no `workspace.nix`; plus the pin agreement
between `flake.nix`'s `home-manager` input and the mod's `nix run`), the
**two-halves resolution** (`resolve_playbook` against a fake conversion — a
built-in `<name>.sh` deleted and a `<name>.nix` beside where it was: a base mod
and an opt-in one both resolving to the generation with no `ModSpec` and no
manifest line, the `requires` on a converted mod still validating, a module the
playbook never names shipped with `enable = false`, a workspace's own `.sh`
shadowing a converted built-in back into the bash half, the box playbook
refusing to see a module at all, and the failure naming all four places it
looked), the **converted `cloudflared`** (an opt-in mod that is now only
`modules/cloudflared.nix`: the flake lists it, a workspace naming it in its own
`mods` resolves to the generation, the module names the creds file's path and no
`[env]` value reaches any rendered `nix/` file, it sources that file rather than
naming it in `EnvironmentFile=` — systemd's parser reads bare `KEY=value` and
drops devbox's `export KEY=…` lines — and it uses home-manager's capitalised
`Unit`/`Service`/`Install` sections; plus `RETIRED_MODS`, which turns a config
still naming the folded-in `cloudflared-tunnel` into an error saying where it
went), the **converted `claude-code` and the two MCP mods** (the base mod
resolving to `modules/claude-code.nix` with no script and no apt `jq` left in
any built-in's `packages`, its `home.activation` entry merging
`permissions.defaultMode` rather than claiming `~/.claude/settings.json` with a
`home.file`, and the MCP pair staying **two** opt-in names — each shipped, each
with its own flag, neither retired — whose `Authorization` header carries the
literal `''${GITHUB_TOKEN}`/`''${LINEAR_API_KEY}`: nix's escape for an
antiquotation, asserted present in the module and unexpanded in the payload file
the box receives, with the `[env]` value itself nowhere in `nix/`), the
**converted `github-auth`** (`modules/github-auth.nix`, agent-scoped now
rather than a root box mod: it claims `~/.config/git/config` — never
`/etc/gitconfig` — with a `home.file`, `dotfiles`/configenv's setup.sh never
touching that path being what removes the ordering constraint the box-mod
version needed, and `repo-bootstrap` still requiring it, now satisfied as a
module rather than across the box/workspace boundary), the **admin
de-customization** (the box playbook shrunk to `dotfiles-packages` + `nix`:
`dotfiles` and `vim-plug` dropped from it, `dotfiles.sh` narrowed to
`runas = ["agent"]` with its admin-only `~/.zshenv` append deleted, and the
privileged account's login shell left on `init-script.sh`'s `/bin/bash`
instead of being switched to zsh — `vim-plug.sh` and `github-auth.sh`
deleted outright), **workspace-local and global nix modules** (JIM-209:
`find_nix_module`/`nix_module_names` against fake workspace/global/built-in
directories — the same workspace > global > built-in precedence
`find_mod_script`/`resolve_mod` give bash mods, out of the SAME three
directories (`mods_dirs`) — a `<name>.nix` lives in the same `mods/` its
`<name>.sh` scripts do at every level, devbox's own `devbox/mods/` included; a
name only the workspace's own `mods_dir` has resolving with no built-in
counterpart at all, one shadowing a built-in of the same name, a workspace's
own `<name>.sh` still winning over its own `<name>.nix` beside it, a global one
shadowed in turn by a workspace's own, and the failure message naming three
directories and both suffixes rather than six paths — plus the
flake itself `readDir`ing `./modules` rather than importing a fixed list,
asserted by its absence of any hardcoded `./modules/<name>.nix` line; and the
packaging invariant that keeps the built-ins shippable, `BUILTIN_MODS_DIR`
being a child of the package directory, holding both suffixes and no name in
both), **`devbox nix check`** (`nix_check_files` turning every discovered
module's flag on — a false flag leaves that module's body inside an unforced
`mkIf`, so a check mirroring a real playbook would evaluate almost nothing —
and widening to a workspace's own modules when there is a workspace;
`cmd_nix_check` against a fake `subprocess`: the nix argv pointed at the
assembled temp directory rather than at `devbox/nix`, `--build` staying opt-in,
a failing step exiting, and the missing-`nix` message; plus `find_workspace_opt`
returning None rather than dying, which is what lets the check run in devbox's
own repo, which is not a workspace),
payload/tarball assembly, `[env]` merge +
`run_envvars` remote command), the per-attachment SSH aliases
(`_refresh_ssh_aliases`: one stanza per file, the collapse rule, per-box
known_hosts), the host→box sync planning (per-side `$HOME`
expansion, agent-anchored guest home, all three anchors — `$HOME`/absolute,
workspace-relative and its refusal under a repo marker, and per-repo, including
picking the right one of several — `include` mirror-unit expansion, rsync argv),
`_select_repo` (by name, from the cwd, the lone-repo fallback, and the refusal
when several are ambiguous),
the libvirt
`<metadata>` stamp/parse + `list` formatting (monkeypatched `virsh`), the
partial listing (`_list_boxes`: a provider whose creds have lapsed becomes a
warning, the second AWS provider is not asked again, other failures still
raise, and `--attached` reuses the one listing), and the
**EC2 provider against a fake EC2 client** (`Ec2Provider` with an injected
`client=`: list/state/address, run-instances request shape, spot, AMI lookup,
subnet capacity fallback, lifecycle), the **aws/Lightsail provider against a
fake Lightsail client** (`LightsailProvider` with an injected `client=`:
create-instances request shape incl. bundle/blueprint/AZ defaults, client-side
tag discovery + paging, the bare-name collision guard, list/state/address,
lifecycle), the jj-remote wiring for `devbox jj
fetch` (`_ensure_jj_remote` add/set-url/noop with a monkeypatched `subprocess`),
`cmd_herdr`'s argv construction (agent alias, trailing-arg pass-through, and the
missing-`herdr` / no-address exits, with `os.execvp` stubbed),
and the **AWS credential handling** (`providers/awsauth.py`: auth-error
duck-typing, the up-front `credential_problem` check against a fake session, the
`aws sso login` command derived from a temp `~/.aws/config`, the TTY/non-TTY/
declined prompt paths, and `cli._run`'s one-line errors) — the exception fakes
carry botocore's real class names, since that naming is what the matcher keys on.
`tests/test_driver.py` is the exception to "the driver can't be tested": it runs
the real `provision-driver.sh` with fake `sudo`/`useradd`/`id`/`getent`/`apt-get`
on PATH, which covers manifest parsing, runas dispatch (including the
privilege drop's target account and `$HOME`), package-install ordering, selective
runs, failure propagation, the isolation assertions, and the whole attach story:
box runs (empty `DEVBOX_AGENT_USER`) creating no account, ownership markers
written/refused/adopted (including a legacy `project=` marker still identifying
its owner, still refusing another workspace, and being retired on attach), the
`loginctl enable-linger` on the attaching account (a box run lingers nobody, and
a box with no user manager to linger gets a note rather than a failed attach),
the `~/.bash_env` nix wiring (both accounts name the store's `/etc/profile.d`
script and their own `~/.nix-profile`, and sourcing the real file puts the
home-manager generation ahead of `~/.local/bin`),
the per-repo stale-checkout warning, per-attachment log directories, and two
workspaces sharing one fake box without disturbing each other. The same fakes
also drive the **detach teardown** — `cli._detach_script`, which is a shell
command built host-side rather than a driver mode, because detach ships no
payload and so has no driver on the box to call, but which is the same
root-owned account surgery: the agent's nix profile, its generation links and
home-manager's GC roots going away while the account's files stay, a box that
never installed nix detaching cleanly, no garbage collection anywhere in the
run, `--purge` taking the whole home with it, and detaching one of two
workspaces leaving the other's profile and ownership marker intact. The fake
`sudo`
executes the command for real, so the control flow is genuine; only the privilege
is simulated. It builds the manifest and the `repos` file with the real
`render_manifest`/`render_repos`, so it also pins the Python→bash format
agreement. `DEVBOX_WORKSPACES_DIR`/`DEVBOX_LEGACY_MARKERS_DIR` exist so the
driver's root-owned markers can be redirected into the fake box, and
`NIX_STATE_DIR` (nix's own variable) does the same for the per-user directories
under `/nix/var/nix` — the seams that only the tests use.

It does not and cannot exercise `box create`/`up`/`stop`/`destroy`/`host`/
`create`/`detach`/`list --attached`/`jj fetch` end-to-end against real infra
(`jj fetch`'s actual `jj git fetch` needs a
booted, provisioned box), the actual behavior of the built-in mods on a real
box (dotfiles, repo bootstrap, the multi-user nix
install and its daemon — though
`repo-bootstrap`'s loop over the `repos` file is plain bash and can be run by
hand against fake `jj`/`git`), **anything nix evaluates** — pytest covers the
rendering and the payload, never the evaluation, but `devbox nix check` does
and it needs no box: it assembles a payload flake in a temp dir and runs `nix
flake check` plus an evaluation of `homeConfigurations.agent-<system>.
activationPackage` against it, with EVERY module enabled (a false
`devbox.mods.<name>.enable` leaves that module's body inside an unforced
`mkIf`, so a check mirroring a real playbook would evaluate almost nothing).
**Run it on any change to a `.nix` file, before committing.** `--build` also
realises the closure, which is minutes of downloads and catches what
evaluation cannot. `--out-dir DIR` keeps the assembled flake to poke at. It
needs `nix` on the host's PATH; failing that, the same check runs on a box
(`devbox ssh`), where the `nix` mod has installed one. The `home-manager
switch` itself, and how a module behaves once activated, stay on-box manual
steps. Also outside pytest: the parts of the driver
that a fake `sudo` can only simulate (real `useradd` flags, sudoers, apt, whether
an agent genuinely lacks privilege), or a
live EC2/Lightsail launch — those need a real libvirt daemon + booted VM, or
real AWS.
Verify such changes by reading the `virsh`/`virt-install`/boto3 calls, the
rendered init script, and the init/mod/driver scripts (`bash -n`, shellcheck)
carefully; with a real libvirt setup, exercise by hand before calling it done:
`devbox box create dev`, then `devbox create` from **two** different workspace
dirs against it — one marked `.devbox/config.toml` inside a repo, one marked
`devbox.toml` beside two repos — and check that each `devbox ssh` lands in its
own account, that the multi-repo one has both checkouts in that one home and
`devbox jj fetch` picks the right one from each, that `sudo -n true` fails as
either agent, that neither can read the other's `~/.config/devbox/env`, that a
second `devbox create` is a clean reconverge, and that `devbox detach` in one
leaves the other untouched. Then, with both still attached, do the **shared-closure
check**: `devbox detach` one of them, run `sudo nix-collect-garbage` as the box's
privileged account (which is what the store's min-free/max-free would eventually
do by itself), and check that the workspace still attached is unharmed — its
`home-manager generations` still resolve, `devbox ssh` still finds the tools its
generation provides, and `devbox sync --mods=home-manager` is still a no-op
reconverge. Two workspaces share most of their closure, so this is what says that
dropping one's roots frees only what nothing else roots.

For the home-manager half, on the same box: that `devbox create` ends with the
`home-manager` mod reporting a switch; that `ssh <box-alias> 'command -v nix'`
and `ssh <box-alias> 'echo $PATH'` — a NON-interactive session, the one Claude
Code's Bash tool uses — find nix and list `~/.nix-profile/bin` ahead of
`~/.local/bin`; that a second `devbox sync --mods=home-manager` is a no-op
reconverge ("No change so reusing latest profile generation"); that both
workspaces have their own generation (`home-manager generations` as each agent,
and `ls -l ~/.nix-profile` pointing into that account's own profile); and that
the box's privileged account has none — it is still on stock apt bash and
unconfigured vim, which is what keeps `devbox box ssh` working whatever a
generation does. The evaluation checks pytest cannot run are `devbox nix
check`'s job, and it runs on the host, before any of this — it assembles the
payload flake itself rather than needing one kept from a real run, which is
what makes it a pre-commit step rather than a post-deploy one.

For the `jj` module, on the same box: `ssh <agent-alias> 'jj --version'` finds
jujutsu from `~/.nix-profile/bin`; `jj config get user.name` in a checkout
returns the workspace's identity; `jj config set --user ui.pager cat` fails,
because home-manager owns `~/.config/jj/config.toml`; and the same setting in
`~/.config/jj/conf.d/local.toml` takes effect, which is the by-hand override
route on the box.

For the vim split, on that same box: `vim` opens with no error in BOTH accounts
- the agent's from the generation, `:scriptnames` there listing the store's
vimrc and the account's own `~/.vimrc`, and `:colorscheme` answering
base16-chalk, which is what proves the store's plugins loaded before the
account's vimrc ran, since configenv's file sets it while being sourced; the
privileged account's is stock apt vim with no `~/.vimrc`, no plugins and no
colorscheme to check, which is the point.

For `github-auth`, on that same box: as the agent, `git config
credential.https://github.com.helper` (or `cat ~/.config/git/config`) names
`gh auth git-credential`, and with a `GITHUB_TOKEN` in `[env]`, `git clone
https://github.com/<private-repo>` succeeds with no prompt; as the privileged
user, the same `git config` shows nothing, and the same clone prompts for
credentials - it has no generation to carry the module.

For Claude Code, on that same box: `command -v claude` as the agent answers
`~/.nix-profile/bin/claude` (not `~/.local/bin/claude`, which an account
attached by an older devbox still holds), `claude --version` matches the
`manifest.json` version at the nixpkgs pin, and `claude doctor` reports no
auto-updater — the package's wrapper `--set`s `DISABLE_AUTOUPDATER=1`, so a
store binary cannot try to replace itself. `jq -r .permissions.defaultMode
~/.claude/settings.json` is `bypassPermissions`, and that file is a REAL file,
not a store symlink: `/config` in a live session still writes it, and a second
`devbox sync --mods=home-manager` neither fails nor reverts what Claude wrote.
Add a `mods = ["claude-github-mcp", "claude-linear-mcp"]` workspace with the two
tokens in `[env]`: `claude mcp list` shows both connected, and
`jq '.mcpServers' ~/.claude.json` holds the literal `${GITHUB_TOKEN}` /
`${LINEAR_API_KEY}` — the header text, never the value, which is the whole point
of the nix escape.

A workspace with `mods = ["cloudflared"]` is the module to exercise by hand,
since only a box runs the real binary: with a `CLOUDFLARE_TOKEN` in `[env]`,
`systemctl --user status cloudflared` is active and `ps` shows no token; with the
key removed the unit is `inactive (dead)` rather than restarting; with a stale
one it runs out of restarts, settles in `failed`, and `devbox status` lists it.
It should also survive `devbox box stop` + `up`, which is what lingering buys.

`tests/conftest.py` just puts the repo root on `sys.path` so `import devbox`
resolves the package (works with or without an editable install). Tests
monkeypatch module-local names where they live (e.g.
`devbox.providers.libvirt.virsh`, `devbox.provision.GLOBAL_MODS_DIR`),
while shared stdlib patches go through the re-exported `devbox.shutil`/
`devbox.subprocess`/`devbox.urllib` (same module singletons everywhere).

## Layout

`devbox/` is a package split by concern:

- `config.py` — global (`GlobalConfig`, incl. `[aws]` shared AWS creds, `[ec2]`
  EC2 globals, `[env]`, `[[sync]]` defaults) + per-workspace (`WorkspaceConfig`:
  `box`, `agent_user`, `[[repos]]`, `mods`, `sync`, `env`, `marker`) config,
  marker resolution, `resolve_instance`/`parse_instance_name`, and
  `resolve_box_spec` — a box's
  shape assembled from `devbox box create` flags plus the global `[defaults]`,
  and **deliberately never persisted** (resources are immutable after create and
  readable from the provider, so a stored copy could only disagree). Hardware
  keys in a workspace's config are a hard error naming the flag to use instead.
  `agent_user` (defaults to the workspace name) is validated at resolve time
  against `USERNAME_RE` / `RESERVED_USERNAMES` — a directory basename is often
  not a legal username, and failing here costs a second instead of a boot; the
  "must differ from the box's privileged account" half can only be checked at
  attach time, since that account belongs to the machine.

  **Two marker spellings, one `Marker`.** `find_workspace` walks up for either
  `<root>/devbox.toml` (the root HOLDS repos) or `<repo>/.devbox/config.toml`
  (the root IS a repo, and the marker hides inside its tree), and returns the
  root, the config path, the mods directory (`mods/` or `.devbox/mods/`) and the
  `root_is_repo` bit whole — so no call site re-derives a path by appending the
  constants. That bit is not decoration: it supplies each `[[repos]]` entry's
  default host path (a nameless entry means "the root itself", legal only under
  `.devbox/`), and it decides whether a bare-relative `[[sync]]` path is
  meaningful (it is symmetric only when the root holds the repos).

  **A workspace holds repos, not a repo.** Each `[[repos]]` entry carries a guest
  `dir` under the agent's home and a host path relative to the root, both
  defaulting to its `name`. They all land in one account, which is exactly what
  makes them share one set of git/jj credentials. `mode` is `clone` or `push`;
  there is no `none` — a workspace with nothing to check out lists no repos.
- `sync.py` — host→box file sync (`[[sync]]`/`[[repos.sync]]`) via `run_sync`
  (per-mirror-unit rsync jobs over this workspace's own alias, anchored at its
  account's home), and `run_envvars` (writes the 0600 `[env]` file into that
  account's home over the box's privileged alias, and wires that account's
  ~/.zshenv where devbox owns it — the privileged user's; an agent's is a file
  of its home-manager generation, `nix/zshenv.nix`, which sources the same
  0600 path). Used by
  `devbox sync --files`/`--envvars`; both auto-run on `create`. Every rule
  carries its own anchor (`ANCHOR_HOME`/`ANCHOR_WORKSPACE`/`ANCHOR_REPO`), so
  reading one tells you where both ends land; all three put the guest end under
  the agent's home.
- `provision.py` — modular provisioning: mod resolution (workspace > global >
  built-in, keyed on `marker.mods_dir`), the two playbooks
  (`box_playbook`/`workspace_playbook`), **the two halves a playbook name can
  resolve to** (`resolve_playbook` → `Playbook(specs, modules)`: a
  `<name>.sh` in any mods directory, or — only when no directory has one — a
  `<name>.nix` in those same directories, discovered by file presence so
  converting a mod is a one-file change with no registry to keep in step and no
  second directory to reach into; scripts win at every
  level of the precedence, so a workspace's own `<name>.sh` gets it its own mod
  and leaves the module inert, and a module name yields no `ModSpec` and no
  manifest line — the driver never hears about it. The box playbook consults no
  module: the flake describes one agent's home), mod metadata
  (`parse_mod_metadata`, which reads the fenced `# /// mod` TOML block with
  stdlib `tomllib`; `ModSpec`, `load_mod_specs`), `scoped_runas` + `render_manifest` (the scope
  filter, applied entirely host-side so the driver never learns about scopes),
  `_box_context`/`_workspace_context` (DEVBOX_* accounts/pubkey/jj context)
  + `resolve_env`/`env_for_owner` (the effective `[env]` map, and one
  account's slice of it), and payload assembly (driver + resolved `mods/*.sh` +
  `context.env` + manifest + `repos`). Ships `provision-driver.sh` and the
  built-in mods, `devbox/mods/*.sh` and `devbox/mods/*.nix` in one directory.
  Delivered over SSH (tar → extract → run driver) by `box create`, `create` and
  `devbox sync --mods`. The box base includes `nix`, which installs the
  multi-user (daemon) nix store with the upstream installer pinned by version —
  box-scoped because the store is the machine's and every attachment shares it,
  with `trusted-users` holding the privileged account only (a trusted user could
  push paths into the shared store for another workspace's agent to run), flakes
  enabled, and no GC timer: the daemon's `min-free`/`max-free` reclaim
  unreferenced paths only when the disk runs short. The checkouts travel as the `repos` file rather than as
  context variables — there can be any number of them, and a shell environment is
  a poor list; `repo-bootstrap` reads it through `DEVBOX_PAYLOAD_DIR`.
  Creds are NOT in the payload — each mod sources the `~/.config/devbox/env` of
  the account that owns them (written by `run_envvars`), which is what keeps the
  payload safe to make agent-readable.
- `nix/` — the home-manager flake SKELETON, the **declarative half** of an
  agent's home. `nix_payload_files` assembles it into a real flake in an
  attachment's payload beside `mods/` — the three skeleton files, the rendered
  `workspace.nix`, and one `modules/<name>.nix` per resolved module out of the
  mods directories — and the `home-manager` mod switches to it. Adding a module
  is one new file and no registration. A mod is a bash script
  or a module here, never both. `flake.nix` pins nixpkgs and home-manager to
  exact revs and carries the module conventions in one comment block; there is
  deliberately **no `flake.lock`** — the pins make the lock nix writes on the
  box deterministic, and no host here has `nix` to generate or verify one
  (adopting a checked-in lock is the natural follow-up once one does).
  `options.nix` declares `devbox.workspace.*` and the free-form
  `devbox.mods.<name>.enable`, and is the only file that imports the rendered
  `workspace.nix` — which `render_workspace_nix` writes per attachment (agent
  user, home, jj identity, and one flag per shipped module), the same job
  `render_manifest` does for the driver, escaping strings for nix rather than
  for the shell. **The flag means "the generation provides this mod"**: true
  only when this attachment's playbook names it and no `<name>.sh` answers to
  the name, false otherwise — so a shadowed mod runs its script and nothing
  else. Every shipped module gets a flag either way, because a missing
  attribute is an evaluation error in nix, not a false. Converting a mod is
  therefore: write `<name>.nix` beside the script, delete `<name>.sh`, and
  leave the playbook tuples alone (the recipe is spelled out in `flake.nix`'s
  conventions block, which is the contract the conversion tickets read) —
  `flake.nix` itself needs no line added: it `readDir`s `./modules` at
  evaluation time rather than importing a fixed list, so a new file is
  discovered, not registered. Home-manager is **agent-only**: a box run ships
  none of this, and the privileged account keeps stock apt bash and
  unconfigured vim - no dotfiles either - so `devbox box ssh` stays the
  machine's debugging entry point regardless of any per-account state.
  **`NIX_DIR` is a SKELETON, not a working flake**: `flake.nix`, `options.nix`
  and `zshenv.nix`, nothing else. It cannot be evaluated where it sits —
  `options.nix` imports the `workspace.nix` only a rendered payload has, and
  there is no `modules/` in the source tree at all. Both appear in an assembled
  payload, and assembling one is the ONLY way to evaluate a module
  (`devbox nix check`, `cli.cmd_nix_check`).
  **Workspace-local and global nix modules ARE supported** (JIM-209): a nix
  module resolves by the same workspace > global > built-in precedence a bash
  mod does, **out of the same three directories** (`provision.mods_dirs`) —
  `<workspace mods dir>/<name>.nix` (either marker shape — see
  `config.Marker.mods_dir`) > `~/.config/devbox/mods/<name>.nix` > devbox's own
  `BUILTIN_MODS_DIR` (see `find_nix_module`) — so a mod that has nothing to do
  with devbox itself never has to live in devbox's own repo to become a
  module; the bash escape hatch (a workspace's own `<name>.sh`) still works
  exactly as it always has, and still wins over any module of the same name,
  which one directory per level makes visible: the two files sit next to each
  other. `find_mod_script` and `find_nix_module` walk that one tuple and differ
  only in the suffix. A module travels to the payload's own
  `nix/modules/<name>.nix` whatever directory it came from, so that assembled
  layout is all nix ever sees — which is what frees every source directory to
  hold both halves. A module needing sibling files of its own isn't
  supported yet — every shipped module is one self-contained file. **No cred
  ever enters a nix expression**:
  /nix/store is world-readable and shared by every attachment. `zshenv.nix` is
  the one module that is not a mod — always in the generation, gated on no
  flag: it owns the agent's `~/.zshenv` (the file every zsh reads, `ssh <box>
  '<cmd>'` included), puts `~/.local/bin` on PATH through `home.sessionPath`,
  and *sources* the 0600 `~/.config/devbox/env` from it rather than carrying a
  value out of it. It deliberately does not enable `programs.zsh`, which would
  make the generation own `~/.zshrc` as well — that file belongs to the
  `dotfiles` bash mod, and one file written by both halves is what breaks the
  second activation.
- `provision-driver.sh` — runs as the privileged user. `ensure_admin` wires that
  account; `ensure_agent` creates the attaching workspace's (idempotent, also
  reachable as `--ensure-agent`, enabling lingering so that account's systemd
  user services outlive an SSH session — a no-op where there is no `loginctl` —
  recording ownership in `/etc/devbox/workspaces/`
  and refusing an account that belongs to another workspace, one with no record
  at all, or one that has acquired sudo). A box last attached by an older devbox
  has its claim under `/etc/devbox/projects/` with a `project=` field; that path
  is read when the current one has nothing, and retired on the next attach. An empty `DEVBOX_AGENT_USER` means a box
  run and skips it entirely. Then each mod's packages are
  apt-installed as the privileged user and its body run via `sudo -u` per
  runas target (default: the agent). Context and creds are re-sourced inside
  the target shell, never passed through `sudo`'s argv — the creds being the
  target account's `[env]` entries, or the privileged user's for a `root` mod
  (`env_owner`). `install_bash_env` writes the per-user `~/.bash_env` that
  `BASH_ENV=/etc/devbox/bash_env` resolves to, and it is the **only** thing
  that puts nix on a non-interactive PATH: the installer wires
  `/etc/profile.d` and the interactive rc files, which `ssh box '<cmd>'` —
  devbox itself, and Claude Code's Bash tool — reads none of. It names the
  store's profile script, the account's `hm-session-vars.sh`, and
  `~/.nix-profile/bin` **ahead of** `~/.local/bin`, so a binary left there by
  a mod that has since been converted cannot shadow the generation. It is also
  how the `home-manager` mod finds `nix` at all, running as the agent under
  `sudo -u`. `rsync` and the login `zsh` stay apt packages for the mirror-image
  reason: a nix profile binary is not reliably on a PATH devbox does not own.
- `initscript.py` — renders the shipped `init-script.sh` (literal
  `%DEVBOX_USER%`/`%DEVBOX_SSH_PUBKEY%`/`%DEVBOX_HOSTNAME%` substitution) into
  the user-data every provider gets. It takes a `BoxSpec`, not a workspace: first
  boot sets up a machine and knows nothing about who will attach to it. The
  script carries just base setup
  (privileged user + SSH key first, then hostname/BASH_ENV/packages) — no
  provisioning payload; provisioning runs over SSH after boot, and a workspace
  extends the box with a mod (there is no workspace first-boot fragment). It does
  **not** create any workspace's account: user-data runs once, so such an account
  could never reach an existing box; the driver owns them. `BASH_ENV` points at
  the shared
  `/etc/devbox/bash_env` stub rather than a per-user path, since
  `/etc/environment` is system-wide and unexpanded. **One init script,
  three providers:** cloud-init runs a `#!` user-data script on libvirt (NoCloud
  seed) and ec2 (`UserData`), and Lightsail runs it natively — a shell script is
  the only user-data all three accept, since Lightsail does not understand
  `#cloud-config`.
- `providers/` — the backend seam. `base.py` (`Provider` ABC, `InstanceSpec`,
  `Instance`); `libvirt.py`; `ec2.py` and `lightsail.py` (the `aws` provider;
  both boto3, lazily imported + injectable client
  for tests); `__init__.py` (`get_provider`, `all_providers` — the AWS-backed
  providers, `AWS_PROVIDERS = ("ec2", "aws")`, gated on
  `[aws]` creds being configured); `awsauth.py` — the single `make_client` both
  AWS providers build through, which resolves the profile's credentials *before*
  the first API call and offers `aws sso login` when they've lapsed
  (`AwsAuthError` carries a finished, user-facing message; `cli._run` also
  catches the server-side auth failures that can't be seen up front, via
  `is_auth_error`). Only `create`/`start`/`stop`/`destroy`/`state`/
  `address`/`list` are per-backend (`get` filters `list` by default); everything
  above (config, render,
  provisioning, ssh/exec, target resolution, list aggregation) is shared, and
  subcommands go through the seam rather than calling `virsh`/boto3 directly.
  Every backend stamps the box's privileged account onto the instance
  (`devbox:admin-user` / libvirt `<user>`), because the host needs it to build
  the SSH alias before it can log in and ask; an unstamped box (created before
  this) falls back to the host's `$USER`.
- `cli.py` — argparse + subcommand handlers + `main` (which dispatches through
  `_run`, the one place AWS credential failures become `error:` lines instead of
  a boto traceback); `__main__.py` for `python -m devbox`. Split in two:
  `cmd_box_*` act on a machine by name, everything else on this workspace's
  account on its box. `devbox create` is porcelain over "ensure the box, then
  attach". `_select_repo` picks which checkout a repo-scoped command (`jj fetch`)
  means: an explicit name, else the one the cwd is inside, else the lone one.
  `list` goes through `_list_boxes`, which reads every provider it can and
  reports the rest as warnings — a read-only global view is worth more partial
  than not at all, and one lapsed AWS session shouldn't hide the local boxes.
  `detach` sends one root-owned teardown to the box (`_detach_script`): the
  ownership marker, the authorized key, the creds, and the account's nix
  profile — the generation profile, every generation link, the `~/.nix-profile`
  the packages go in and home-manager's own GC roots, all of which sit under
  the account's home on the nix the box installs, plus the per-user directories
  under `/nix/var/nix` for the other layouts a root can sit in. **A generation
  is a GC root**, so an account that keeps one keeps its whole store closure,
  and a box that has hosted a dozen workspaces would fill with the closures of
  workspaces nobody uses. The files stay (a generation is derived, and
  `devbox create` switches the account back to one); `--purge` takes the home
  too. It drops the roots and runs **no** `nix-collect-garbage`: collection is
  the box's min-free/max-free policy (see the `nix` mod), and a sweep at
  teardown would spend minutes discarding byproducts the box's other
  workspaces are about to reuse, at a moment whoever detached happened to pick.
- `__init__.py` — re-exports a flat `devbox.X` API for convenience/tests.

`pyproject.toml` (hatchling) declares the `devbox.cli:main` entry point and the
one runtime dep, `boto3` (the ec2 + aws/Lightsail providers). It force-includes
nothing: every data file devbox resolves through `Path(__file__).parent` lives
inside the package — `devbox/init-script.sh`, `devbox/provision-driver.sh`,
`devbox/mods/` (both halves) and the `devbox/nix/` skeleton — so
`packages = ["devbox"]` carries them all and a new mod, script or module, is
one file and no build change. Keep it that way: a data file outside `devbox/`
resolves under an editable install and is silently absent from a wheel, which
is the shape of the bug that left `mods/*.sh` unshipped while it sat at the
repo root.
