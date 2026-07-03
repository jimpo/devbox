# AGENTS.md

## Dev loop

```bash
uv sync                          # create .venv with dev deps (pytest)
uv run pytest                    # run the test suite
uv tool install --editable .     # point the live `devbox` command at this checkout
                                  # (editable install - no reinstall needed after further edits)
```

## Testing scope

`tests/` only exercises pure logic: global/project config loading and
merging (`GlobalConfig.load`, `resolve_project`), marker walk-up resolution,
and cloud-config token substitution + YAML merging (`merge_cloud_config`,
`render_cloud_config`). It does not and cannot exercise
`create`/`up`/`stop`/`destroy`/`host` end-to-end, or the actual content of
`common-cloud-config.yaml`'s provisioning script (jj install, dotfiles,
Claude Code, repo bootstrap) — those need a real libvirt daemon and a booted
VM, which isn't available in CI or a sandboxed agent environment. Verify
changes to those subcommands, or to `common-cloud-config.yaml`, by reading
the `virsh`/`virt-install` invocations and the rendered cloud-config
carefully; if you have access to a real libvirt setup, exercise them by
hand (`devbox create` + `devbox status` through to `cloud-init status --long`
reporting `done`, then `devbox ssh` in and check the specific thing you
changed) before calling a change done.

`tests/conftest.py` loads `devbox.py` via `importlib` (it's a flat script,
not a package) and registers it as `sys.modules["devbox"]` — test files
just `import devbox` after that.

## Layout

Almost everything lives in `devbox.py` (no submodules) plus
`common-cloud-config.yaml`, the shared cloud-config template merged onto
every project's optional `.devbox/cloud-config.yaml` at `create` time (see
README's "How cloud-config merging works"). `pyproject.toml` exists
to make `uv tool install --editable .` work (`project.scripts` entry
point), declare `pytest` as a dev dependency, and declare the one real
runtime dependency (`pyyaml`, for the cloud-config merge) — it is not a
general packaging setup, and there is no reason to split `devbox.py` into a
package unless it grows substantially.
