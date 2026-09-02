# AGENTS.md

## Dev loop

```bash
uv sync                          # create .venv with dev deps (pytest)
uv run pytest                    # run the test suite
uv tool install --editable .     # point the live `devbox` command at this checkout
                                  # (editable install - no reinstall needed after further edits)
```

## Testing scope

`tests/` exercises pure logic and provider paths reachable without real infra:
global/workspace config loading + merging (`GlobalConfig.load`,
`resolve_workspace`, `resolve_instance`, `parse_instance_name`), the
box/workspace split (`resolve_box_spec` from argv, the rejection of box-level
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
two playbooks and `scoped_runas`'s narrowing of each mod's targets (including
`github-auth`'s box scope and its `/etc/gitconfig` target — what keeps the
credential helper out of the `~/.gitconfig` that `dotfiles` overwrites — and
`repo-bootstrap` requiring it across the playbook boundary), manifest
rendering, `render_repos` + the `repos` payload file, env-file quoting,
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
per-repo stale-checkout warning, per-attachment log directories, and two
workspaces sharing one fake box without disturbing each other. The fake `sudo`
executes the command for real, so the control flow is genuine; only the privilege
is simulated. It builds the manifest and the `repos` file with the real
`render_manifest`/`render_repos`, so it also pins the Python→bash format
agreement. `DEVBOX_WORKSPACES_DIR`/`DEVBOX_LEGACY_MARKERS_DIR` exist so the
driver's root-owned markers can be redirected into the fake box — the one seam in
the driver that only the tests use.

It does not and cannot exercise `box create`/`up`/`stop`/`destroy`/`host`/
`create`/`detach`/`list --attached`/`jj fetch` end-to-end against real infra
(`jj fetch`'s actual `jj git fetch` needs a
booted, provisioned box), the actual behavior of the built-in mods on a real
box (jj install, dotfiles, Claude Code, repo bootstrap — though
`repo-bootstrap`'s loop over the `repos` file is plain bash and can be run by
hand against fake `jj`/`git`), the parts of the driver
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
leaves the other untouched.

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
  account's home over the box's privileged alias, wires its ~/.zshenv). Used by
  `devbox sync --files`/`--envvars`; both auto-run on `create`. Every rule
  carries its own anchor (`ANCHOR_HOME`/`ANCHOR_WORKSPACE`/`ANCHOR_REPO`), so
  reading one tells you where both ends land; all three put the guest end under
  the agent's home.
- `provision.py` — modular provisioning: mod resolution (workspace > global >
  built-in, keyed on `marker.mods_dir`), the two playbooks
  (`box_playbook`/`workspace_playbook`), mod metadata
  (`parse_mod_metadata`, which reads the fenced `# /// mod` TOML block with
  stdlib `tomllib`; `ModSpec`, `load_mod_specs`), `scoped_runas` + `render_manifest` (the scope
  filter, applied entirely host-side so the driver never learns about scopes),
  `_box_context`/`_workspace_context` (DEVBOX_* accounts/pubkey/jj context)
  + `resolve_env`/`env_for_owner` (the effective `[env]` map, and one
  account's slice of it), and payload assembly (driver + resolved `mods/*.sh` +
  `context.env` + manifest + `repos`). Ships `provision-driver.sh` + `mods/*.sh`.
  Delivered over SSH (tar → extract → run driver) by `box create`, `create` and
  `devbox sync --mods`. The checkouts travel as the `repos` file rather than as
  context variables — there can be any number of them, and a shell environment is
  a poor list; `repo-bootstrap` reads it through `DEVBOX_PAYLOAD_DIR`.
  Creds are NOT in the payload — each mod sources the `~/.config/devbox/env` of
  the account that owns them (written by `run_envvars`), which is what keeps the
  payload safe to make agent-readable.
- `provision-driver.sh` — runs as the privileged user. `ensure_admin` wires that
  account; `ensure_agent` creates the attaching workspace's (idempotent, also
  reachable as `--ensure-agent`, recording ownership in `/etc/devbox/workspaces/`
  and refusing an account that belongs to another workspace, one with no record
  at all, or one that has acquired sudo). A box last attached by an older devbox
  has its claim under `/etc/devbox/projects/` with a `project=` field; that path
  is read when the current one has nothing, and retired on the next attach. An empty `DEVBOX_AGENT_USER` means a box
  run and skips it entirely. Then each mod's packages are
  apt-installed as the privileged user and its body run via `sudo -u` per
  runas target (default: the agent). Context and creds are re-sourced inside
  the target shell, never passed through `sudo`'s argv — the creds being the
  target account's `[env]` entries, or the privileged user's for a `root` mod
  (`env_owner`).
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
- `__init__.py` — re-exports a flat `devbox.X` API for convenience/tests.

`pyproject.toml` (hatchling) declares the `devbox.cli:main` entry point,
force-includes the package data files (`devbox/init-script.sh`,
`devbox/provision-driver.sh`) into the wheel, and declares the one runtime dep, `boto3` (the ec2 +
aws/Lightsail providers).
