# Development

Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing provisioning, mods or
the nix flake.

## Dev loop

```bash
uv sync                          # .venv with dev deps (pytest)
uv run pytest                    # the test suite
uv run devbox nix check          # evaluate the home-manager modules (needs nix)
uv tool install --editable .     # point the live `devbox` at this checkout
```

**Run `devbox nix check` on any change to a `.nix` file, before committing.**
pytest never evaluates nix.

## Packaging

Every data file devbox resolves through `Path(__file__).parent` lives inside
`devbox/` (`init-script.sh`, `provision-driver.sh`, `mods/`, `nix/`), so
`packages = ["devbox"]` ships them with no build entry. Keep it that way: a data
file outside the package works under an editable install and is silently missing
from a wheel.

## Tests

`tests/` covers pure logic and provider paths reachable without real infra:

- **Config**: global/workspace loading and merging, both marker spellings and
  walk-up, `[[repos]]` validation, `secrets.env`, box-spec resolution and the
  rejection of hardware keys, username defaults and validation.
- **Provisioning**: mod resolution and precedence for both halves, `# /// mod`
  parsing, `requires` ordering, `scoped_runas`, the playbooks' exact contents,
  manifest/repos/env rendering, `RETIRED_MODS`.
- **Nix rendering**: `render_workspace_nix`, `nix_payload_files`, the skeleton's
  contents, pins agreeing between `flake.nix` and `home-manager.sh`, and per-mod
  invariants (e.g. no `[env]` value in any payload file, the MCP headers'
  unexpanded `${…}`).
- **First boot**: `init-script.sh` rendering, and its `useradd` against fake
  `id`/`getent`.
- **Sync and SSH**: rsync planning for all three anchors, `include` expansion, SSH
  alias files, `_select_repo`.
- **Providers**: libvirt against a monkeypatched `virsh`; `Ec2Provider` and
  `LightsailProvider` with an injected fake `client=`; `awsauth` against a fake
  session. The exception fakes carry botocore's real class names, since the
  matcher keys on them.
- **CLI**: `cmd_nix_check`, `cmd_herdr`, `_ensure_jj_remote`, `_list_boxes`, with
  `subprocess`/`os.execvp` faked.
- **The driver** (`tests/test_driver.py`): runs the real `provision-driver.sh` with
  fake `sudo`/`useradd`/`id`/`getent`/`apt-get` on PATH. The fake `sudo` executes
  for real, so control flow is genuine and only privilege is simulated. It covers
  manifest parsing, runas dispatch, ownership markers, lingering, `~/.bash_env`,
  per-attachment logs, two workspaces sharing one box, and `cli._detach_script`'s
  teardown. It renders the manifest with the real `render_manifest`, pinning the
  Python→bash format.

Conventions:
- Monkeypatch names where they're used (`devbox.providers.libvirt.virsh`,
  `devbox.provision.GLOBAL_MODS_DIR`). Patch shared stdlib through the
  re-exported `devbox.subprocess`/`devbox.shutil`/`devbox.urllib`.
- `DEVBOX_WORKSPACES_DIR`, `DEVBOX_LEGACY_MARKERS_DIR` and `NIX_STATE_DIR` exist
  only so the tests can redirect the driver's root-owned paths.
- `tests/conftest.py` just puts the repo root on `sys.path`.

## `devbox nix check`

Assembles a payload flake in a temp dir (the skeleton, a placeholder
`workspace.nix`, every resolvable module) and forces
`homeConfigurations.agent-<system>.activationPackage`.

- **Every module is enabled.** A false flag leaves a module's body in an unforced
  `mkIf`, so mirroring a real playbook would evaluate almost nothing.
- Inside a workspace it includes that workspace's own modules too.
- It reaches the network: `configenv` is a branch, re-resolved each run.
- `--build` realises the closure (minutes). Only a build catches two packages
  claiming one profile path, or two `home.file`s claiming one home path.
- `--out-dir DIR` keeps the flake. Without `nix` on the host, run it on a box.

## Manual verification

Not testable here: real `virsh`/`virt-install`/boto3 calls, `useradd` flags,
sudoers, apt, the nix install, `home-manager switch`, and how the mods behave on
a real box. For those, read the calls carefully, `bash -n` and shellcheck the
scripts, and exercise by hand:

**Attachment** — `devbox box create dev`, then `devbox create` from two workspaces
against it: one `.devbox/config.toml` inside a repo, one `devbox.toml` beside two
repos.
- Each `devbox ssh` lands in its own account; the multi-repo one has both
  checkouts, and `devbox jj fetch` picks the right one from inside each.
- `sudo -n true` fails as either agent; neither can read the other's
  `~/.config/devbox/env`.
- A second `devbox create` reconverges cleanly; `devbox detach` in one leaves the
  other untouched.
- **Shared closure**: detach one, `sudo nix-collect-garbage` as admin, then the
  other's `home-manager generations` still resolve, its tools are on PATH, and
  `devbox sync --mods` is a no-op.

**Generation**
- `create` ends with `home-manager` reporting a switch; running it twice changes
  nothing and creates no `*.devbox-backup`.
- `ssh <agent-alias> 'echo $PATH'` (non-interactive) lists `~/.nix-profile/bin`
  before `~/.local/bin`, and `'echo $SOME_ENV_KEY'` returns an `[env]` value.
- The privileged account has no generation: stock bash, unconfigured vim.

**Per mod**
- `jj`: `jj config get user.name` is the workspace identity; a
  `~/.config/jj/conf.d/local.toml` setting overrides it.
- `dotfiles`: `~/.vimrc`, `~/.zshrc`, `~/.config/{jj,nvim}` … are store symlinks;
  interactive zsh shows the `jimpo` theme; vim's `:colorscheme` is base16-chalk.
- `github-auth`: with `GITHUB_TOKEN`, a private `git clone` needs no prompt as the
  agent and prompts as admin.
- `claude-code`: `claude doctor` shows the auto-updater on;
  `permissions.defaultMode` is `bypassPermissions` and survives a reconverge. The
  MCP mods: `claude mcp list` shows both connected, and `~/.claude.json` holds the
  literal `${…}`.
- `codex`: `codex update` re-runs the standalone installer, and `~/.zshenv` stays
  a store symlink.
- `nodejs`: `node` is the LTS, and `npm install -g <cli>` succeeds and lands on the
  non-interactive PATH.
- `cloudflared`: with a token the user unit is active and no token is visible in
  `ps`; without one it's inactive; a bad one ends `failed` and shows in
  `devbox status`; it survives `box stop` + `up`.
