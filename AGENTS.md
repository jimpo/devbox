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
and cloud-init token substitution (`render_cloud_init`). It does not and
cannot exercise `create`/`up`/`stop`/`destroy`/`host` end-to-end — those
need a real libvirt daemon and a booted VM, which isn't available in CI or a
sandboxed agent environment. Verify changes to those subcommands by
reading the `virsh`/`virt-install` invocations carefully; if you have access
to a real libvirt setup, exercise them by hand before calling a change done.

`tests/conftest.py` loads `devbox.py` via `importlib` (it's a flat script,
not a package) and registers it as `sys.modules["devbox"]` — test files
just `import devbox` after that.

## Layout

Everything lives in `devbox.py` (no submodules). `pyproject.toml` exists
only to make `uv tool install --editable .` work (`project.scripts` entry
point) and to declare `pytest` as a dev dependency — it is not a general
packaging setup, and there is no reason to split `devbox.py` into a package
unless it grows substantially.
