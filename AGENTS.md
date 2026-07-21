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
`resolve_instance`), marker walk-up, cloud-config token substitution + YAML
merging, the libvirt `<metadata>` stamp/parse + `list` formatting (monkeypatched
`virsh`), and the **AWS provider against a fake EC2 client** (`AwsProvider` with
an injected `client=`: list/state/address, run-instances request shape, spot,
AMI lookup, subnet capacity fallback, lifecycle), and the jj-remote wiring for
`devbox sync` (`_ensure_jj_remote` add/set-url/noop with a monkeypatched
`subprocess`). It does not and cannot exercise `create`/`up`/`stop`/`destroy`/
`host`/`provision`/`list`/`sync` end-to-end against real infra (`sync`'s actual
`jj git fetch` needs a booted, provisioned instance), the actual content of
`provision-common.sh` (jj install,
dotfiles, Claude Code, repo bootstrap), or a live EC2 launch — those need a real
libvirt daemon + booted VM, or real AWS. Verify such changes by reading the
`virsh`/`virt-install`/boto3 calls and the rendered cloud-config carefully; with
a real libvirt setup, exercise by hand (`devbox create` + `devbox status`
through to `cloud-init status --long` reporting `done`, then `devbox ssh`; for
provisioning edits, `devbox provision` and re-check) before calling it done.

`tests/conftest.py` just puts the repo root on `sys.path` so `import devbox`
resolves the package (works with or without an editable install). Tests
monkeypatch module-local names where they live (e.g.
`devbox.providers.libvirt.virsh`, `devbox.cloudconfig.COMMON_CLOUD_CONFIG_PATH`),
while shared stdlib patches go through the re-exported `devbox.shutil`/
`devbox.subprocess`/`devbox.urllib` (same module singletons everywhere).

## Layout

`devbox/` is a package split by concern:

- `config.py` — global (`GlobalConfig`, incl. `[aws]`) + per-project
  (`ProjectConfig`, incl. `[aws]`) config, marker resolution, `resolve_instance`.
- `cloudconfig.py` — token substitution, common+project merge, and the render of
  `common-cloud-config.yaml` + the standalone re-runnable `provision-common.sh`
  (both ship inside the package). One cloud-config, two delivery paths (libvirt
  NoCloud seed / EC2 user-data).
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
force-includes the two package data files into the wheel, and declares runtime
deps `pyyaml` (cloud-config merge) + `boto3` (AWS provider).
