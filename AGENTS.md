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
`resolve_instance`, `parse_instance_name`), marker walk-up, the first-boot
init-script render (token substitution into the shipped `init-script.sh`), the
modular-provisioning logic (mod resolution
precedence, env-file quoting, payload/tarball assembly, `[env]` merge +
`run_envvars` remote command), the host→box sync planning (per-side `$HOME`
expansion, repo-relative anchoring, `include` mirror-unit expansion, rsync argv),
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
It does not and cannot exercise `create`/`up`/`stop`/`destroy`/`host`/`provision`/
`list`/`jj fetch` end-to-end against real infra (`jj fetch`'s actual `jj git
fetch` needs a
booted, provisioned instance), the actual behavior of the built-in mods or the
driver on a real box (jj install, dotfiles, Claude Code, repo bootstrap), or a
live EC2/Lightsail launch — those need a real libvirt daemon + booted VM, or
real AWS.
Verify such changes by reading the `virsh`/`virt-install`/boto3 calls, the
rendered init script, and the init/mod/driver scripts (`bash -n`, shellcheck)
carefully; with a real libvirt setup, exercise by hand (`devbox create` +
`devbox status` through to a fully provisioned box (create blocks through it),
then `devbox ssh`; for provisioning edits, `devbox sync --mods` and re-check)
before calling it done.

`tests/conftest.py` just puts the repo root on `sys.path` so `import devbox`
resolves the package (works with or without an editable install). Tests
monkeypatch module-local names where they live (e.g.
`devbox.providers.libvirt.virsh`, `devbox.provision.GLOBAL_MODS_DIR`),
while shared stdlib patches go through the re-exported `devbox.shutil`/
`devbox.subprocess`/`devbox.urllib` (same module singletons everywhere).

## Layout

`devbox/` is a package split by concern:

- `config.py` — global (`GlobalConfig`, incl. `[aws]` shared AWS creds, `[ec2]`
  EC2 globals, `[env]`) + per-project (`ProjectConfig`, incl. `[ec2]`,
  `[lightsail]`, `mods`, `sync`, `env`, `marker_dir`) config,
  marker resolution, `resolve_instance`/`parse_instance_name`.
- `sync.py` — host→box file sync (`[[sync]]`/`[[repo.sync]]`) via `run_sync`
  (per-mirror-unit rsync jobs over the `devbox-<name>` alias), and `run_envvars`
  (writes the box's 0600 `[env]` file over SSH, wires ~/.zshenv). Used by
  `devbox sync --files`/`--envvars`; both auto-run on `create`.
- `provision.py` — modular provisioning: mod resolution (project > global >
  built-in), the base playbook, `_provision_context` (DEVBOX_* jj/repo context)
  + `resolve_env` (the effective `[env]` map), and payload assembly (driver +
  resolved `mods/*.sh` + `context.env` + manifest). Ships `provision-driver.sh`
  + `mods/*.sh`. Delivered over SSH (tar → extract → run driver) by `create` and
  `devbox sync --mods`. Creds are NOT in the payload — the driver sources the
  box's `~/.config/devbox/env` (written by `run_envvars`).
- `initscript.py` — renders the shipped `init-script.sh` (literal
  `%DEVBOX_USER%`/`%DEVBOX_SSH_PUBKEY%`/`%DEVBOX_HOSTNAME%` substitution) into
  the user-data every provider gets. The script carries just base setup (login
  user + SSH key first, then hostname/BASH_ENV/packages) — no provisioning
  payload; provisioning runs over SSH after boot, and a project extends the box
  with a mod (there is no project first-boot fragment). **One init script,
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
  `address`/`list` are per-backend; everything above (config, render,
  provisioning, ssh/exec, target resolution, list aggregation) is shared, and
  subcommands go through the seam rather than calling `virsh`/boto3 directly.
- `cli.py` — argparse + subcommand handlers + `main` (which dispatches through
  `_run`, the one place AWS credential failures become `error:` lines instead of
  a boto traceback); `__main__.py` for `python -m devbox`.
- `__init__.py` — re-exports a flat `devbox.X` API for convenience/tests.

`pyproject.toml` (hatchling) declares the `devbox.cli:main` entry point,
force-includes the package data files (`devbox/init-script.sh`,
`devbox/provision-driver.sh`) into the wheel, and declares the one runtime dep, `boto3` (the ec2 +
aws/Lightsail providers).
