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
`resolve_instance`, `parse_instance_name`), marker walk-up, cloud-config token
substitution + YAML merging, the modular-provisioning logic (mod resolution
precedence, env-file quoting, payload/tarball assembly, `[env]` merge +
`run_envvars` remote command), the host→box sync planning (per-side `$HOME`
expansion, repo-relative anchoring, `include` mirror-unit expansion, rsync argv),
the libvirt
`<metadata>` stamp/parse + `list` formatting (monkeypatched `virsh`), and the
**AWS provider against a fake EC2 client** (`AwsProvider` with an injected
`client=`: list/state/address, run-instances request shape, spot, AMI lookup,
subnet capacity fallback, lifecycle), and the jj-remote wiring for `devbox jj
fetch` (`_ensure_jj_remote` add/set-url/noop with a monkeypatched `subprocess`).
It does not and cannot exercise `create`/`up`/`stop`/`destroy`/`host`/`provision`/
`list`/`jj fetch` end-to-end against real infra (`jj fetch`'s actual `jj git
fetch` needs a
booted, provisioned instance), the actual behavior of the built-in mods or the
driver on a real box (jj install, dotfiles, Claude Code, repo bootstrap), or a
live EC2 launch — those need a real libvirt daemon + booted VM, or real AWS.
Verify such changes by reading the `virsh`/`virt-install`/boto3 calls, the
rendered cloud-config, and the mod/driver scripts (`bash -n`, shellcheck)
carefully; with a real libvirt setup, exercise by hand (`devbox create` +
`devbox status` through to a fully provisioned box (create blocks through it),
then `devbox ssh`; for provisioning edits, `devbox sync --mods` and re-check)
before calling it done.

`tests/conftest.py` just puts the repo root on `sys.path` so `import devbox`
resolves the package (works with or without an editable install). Tests
monkeypatch module-local names where they live (e.g.
`devbox.providers.libvirt.virsh`, `devbox.cloudconfig.COMMON_CLOUD_CONFIG_PATH`),
while shared stdlib patches go through the re-exported `devbox.shutil`/
`devbox.subprocess`/`devbox.urllib` (same module singletons everywhere).

## Layout

`devbox/` is a package split by concern:

- `config.py` — global (`GlobalConfig`, incl. `[aws]`, `[env]`) + per-project
  (`ProjectConfig`, incl. `[aws]`, `mods`, `sync`, `env`, `marker_dir`) config,
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
- `cloudconfig.py` — token substitution (`%DEVBOX_USER%`/`%DEVBOX_SSH_PUBKEY%`) +
  common+project merge only. The cloud-config carries just base setup
  (user/packages/BASH_ENV) — no provisioning payload; provisioning runs over SSH
  after boot. One cloud-config, two providers (libvirt NoCloud seed / EC2 user-data).
- `providers/` — the backend seam. `base.py` (`Provider` ABC, `InstanceSpec`,
  `Instance`); `libvirt.py`; `aws.py` (boto3, lazily imported + injectable client
  for tests); `__init__.py` (`get_provider`, `all_providers` — AWS gated on
  `[aws]` being configured). Only `create`/`start`/`stop`/`destroy`/`state`/
  `address`/`list` are per-backend; everything above (config, render,
  provisioning, ssh/exec, target resolution, list aggregation) is shared, and
  subcommands go through the seam rather than calling `virsh`/boto3 directly.
- `cli.py` — argparse + subcommand handlers + `main`; `__main__.py` for
  `python -m devbox`.
- `__init__.py` — re-exports a flat `devbox.X` API for convenience/tests.

`pyproject.toml` (hatchling) declares the `devbox.cli:main` entry point,
force-includes the package data files (`common-cloud-config.yaml`,
`provision-driver.sh`, `mods/`) into the wheel, and declares runtime deps
`pyyaml` (cloud-config merge) + `boto3` (AWS provider).
