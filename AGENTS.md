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
global/project config loading + merging (`GlobalConfig.load`, `resolve_project`,
`resolve_instance`, `parse_instance_name`), the box/project split
(`resolve_box_spec` from argv, the rejection of box-level keys in a project's
config, `box` defaulting, `agent_user` defaulting + username/reserved-name
validation), marker
walk-up, the first-boot
init-script render (token substitution into the shipped `init-script.sh`, now
box-scoped), the
modular-provisioning logic (mod resolution
precedence, mod metadata headers — `# requires:`/`# runas:`/`# packages:` — the
two playbooks and `scoped_runas`'s narrowing of each mod's targets, manifest
rendering, env-file quoting, payload/tarball assembly, `[env]` merge +
`run_envvars` remote command), the per-attachment SSH aliases
(`_refresh_ssh_aliases`: one stanza per file, the collapse rule, per-box
known_hosts), the host→box sync planning (per-side `$HOME`
expansion, agent-anchored guest home, repo-relative anchoring, `include`
mirror-unit expansion, rsync argv),
the libvirt
`<metadata>` stamp/parse + `list` formatting (monkeypatched `virsh`), and the
**EC2 provider against a fake EC2 client** (`Ec2Provider` with an injected
`client=`: list/state/address, run-instances request shape, spot, AMI lookup,
subnet capacity fallback, lifecycle), the **aws/Lightsail provider against a
fake Lightsail client** (`LightsailProvider` with an injected `client=`:
create-instances request shape incl. bundle/blueprint/AZ defaults, client-side
tag discovery + paging, the bare-name collision guard, list/state/address,
lifecycle), the jj-remote wiring for `devbox jj
fetch` (`_ensure_jj_remote` add/set-url/noop with a monkeypatched `subprocess`),
and the **AWS credential handling** (`providers/awsauth.py`: auth-error
duck-typing, the up-front `credential_problem` check against a fake session, the
`aws sso login` command derived from a temp `~/.aws/config`, the TTY/non-TTY/
declined prompt paths, and `cli._run`'s one-line errors) — the exception fakes
carry botocore's real class names, since that naming is what the matcher keys on.
`tests/test_driver.py` is the exception to "the driver can't be tested": it runs
the real `provision-driver.sh` with fake `sudo`/`useradd`/`id`/`getent`/`apt-get`
on PATH, which covers manifest parsing, `# runas:` dispatch (including the
privilege drop's target account and `$HOME`), `# packages:` ordering, selective
runs, failure propagation, the isolation assertions, and the whole attach story:
box runs (empty `DEVBOX_AGENT_USER`) creating no account, ownership markers
written/refused/adopted, per-attachment log directories, and two projects sharing
one fake box without disturbing each other. The fake `sudo` executes the command
for real, so the control flow is genuine; only the privilege is simulated. It
builds the manifest with the real `render_manifest`, so it also pins the
Python→bash format agreement. `DEVBOX_PROJECTS_DIR` exists so the driver's
root-owned markers can be redirected into the fake box — the one seam in the
driver that only the tests use.

It does not and cannot exercise `box create`/`up`/`stop`/`destroy`/`host`/
`create`/`detach`/`list --attached`/`jj fetch` end-to-end against real infra
(`jj fetch`'s actual `jj git fetch` needs a
booted, provisioned box), the actual behavior of the built-in mods on a real
box (jj install, dotfiles, Claude Code, repo bootstrap), the parts of the driver
that a fake `sudo` can only simulate (real `useradd` flags, sudoers, apt, whether
an agent genuinely lacks privilege), or a
live EC2/Lightsail launch — those need a real libvirt daemon + booted VM, or
real AWS.
Verify such changes by reading the `virsh`/`virt-install`/boto3 calls, the
rendered init script, and the init/mod/driver scripts (`bash -n`, shellcheck)
carefully; with a real libvirt setup, exercise by hand before calling it done:
`devbox box create dev`, then `devbox create` from **two** different project
dirs against it, and check that each `devbox ssh` lands in its own account, that
`sudo -n true` fails as either agent, that neither can read the other's
`~/.config/devbox/env`, that a second `devbox create` is a clean reconverge, and
that `devbox detach` in one leaves the other untouched.

`tests/conftest.py` just puts the repo root on `sys.path` so `import devbox`
resolves the package (works with or without an editable install). Tests
monkeypatch module-local names where they live (e.g.
`devbox.providers.libvirt.virsh`, `devbox.provision.GLOBAL_MODS_DIR`),
while shared stdlib patches go through the re-exported `devbox.shutil`/
`devbox.subprocess`/`devbox.urllib` (same module singletons everywhere).

## Layout

`devbox/` is a package split by concern:

- `config.py` — global (`GlobalConfig`, incl. `[aws]` shared AWS creds, `[ec2]`
  EC2 globals, `[env]`) + per-project (`ProjectConfig`: `box`, `agent_user`,
  `[repo]`, `mods`, `sync`, `env`, `marker_dir`) config, marker resolution,
  `resolve_instance`/`parse_instance_name`, and `resolve_box_spec` — a box's
  shape assembled from `devbox box create` flags plus the global `[defaults]`,
  and **deliberately never persisted** (resources are immutable after create and
  readable from the provider, so a stored copy could only disagree). Hardware
  keys in a project's config are a hard error naming the flag to use instead.
  `agent_user` (defaults to the project name) is validated at resolve time
  against `USERNAME_RE` / `RESERVED_USERNAMES` — a directory basename is often
  not a legal username, and failing here costs a second instead of a boot; the
  "must differ from the box's privileged account" half can only be checked at
  attach time, since that account belongs to the machine.
- `sync.py` — host→box file sync (`[[sync]]`/`[[repo.sync]]`) via `run_sync`
  (per-mirror-unit rsync jobs over this project's own alias, anchored at its
  account's home), and `run_envvars` (writes the 0600 `[env]` file into that
  account's home over the box's privileged alias, wires its ~/.zshenv). Used by
  `devbox sync --files`/`--envvars`; both auto-run on `create`.
- `provision.py` — modular provisioning: mod resolution (project > global >
  built-in), the two playbooks (`box_playbook`/`project_playbook`), mod metadata
  (`parse_header` and the `# requires:`/`# runas:`/`# packages:` wrappers,
  `ModSpec`, `load_mod_specs`), `scoped_runas` + `render_manifest` (the scope
  filter, applied entirely host-side so the driver never learns about scopes),
  `_box_context`/`_project_context` (DEVBOX_* accounts/pubkey/jj/repo context)
  + `resolve_env`/`env_for_owner` (the effective `[env]` map, and one
  account's slice of it), and payload assembly (driver + resolved `mods/*.sh` +
  `context.env` + manifest). Ships `provision-driver.sh` + `mods/*.sh`. Delivered
  over SSH (tar → extract → run driver) by `box create`, `create` and
  `devbox sync --mods`.
  Creds are NOT in the payload — each mod sources the `~/.config/devbox/env` of
  the account that owns them (written by `run_envvars`), which is what keeps the
  payload safe to make agent-readable.
- `provision-driver.sh` — runs as the privileged user. `ensure_admin` wires that
  account; `ensure_agent` creates the attaching project's (idempotent, also
  reachable as `--ensure-agent`, recording ownership in `/etc/devbox/projects/`
  and refusing an account that belongs to another project, one with no record at
  all, or one that has acquired sudo). An empty `DEVBOX_AGENT_USER` means a box
  run and skips it entirely. Then each mod's `# packages:` are
  apt-installed as the privileged user and its body run via `sudo -u` per
  `# runas:` target (default: the agent). Context and creds are re-sourced inside
  the target shell, never passed through `sudo`'s argv — the creds being the
  target account's `[env]` entries, or the privileged user's for a `root` mod
  (`env_owner`).
- `initscript.py` — renders the shipped `init-script.sh` (literal
  `%DEVBOX_USER%`/`%DEVBOX_SSH_PUBKEY%`/`%DEVBOX_HOSTNAME%` substitution) into
  the user-data every provider gets. It takes a `BoxSpec`, not a project: first
  boot sets up a machine and knows nothing about who will attach to it. The
  script carries just base setup
  (privileged user + SSH key first, then hostname/BASH_ENV/packages) — no
  provisioning payload; provisioning runs over SSH after boot, and a project
  extends the box with a mod (there is no project first-boot fragment). It does
  **not** create any project's account: user-data runs once, so such an account
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
  `cmd_box_*` act on a machine by name, everything else on this project's account
  on its box. `devbox create` is porcelain over "ensure the box, then attach".
- `__init__.py` — re-exports a flat `devbox.X` API for convenience/tests.

`pyproject.toml` (hatchling) declares the `devbox.cli:main` entry point,
force-includes the package data files (`devbox/init-script.sh`,
`devbox/provision-driver.sh`) into the wheel, and declares the one runtime dep, `boto3` (the ec2 +
aws/Lightsail providers).
