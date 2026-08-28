#!/usr/bin/env bash
#
# /// mod
# requires = ["dotfiles-packages", "tree-sitter-cli"]
# runas = ["admin", "agent"]
# ///
#
# mod: dotfiles - the per-user half of the configenv setup from
# github.com/jimpo/configenv: its setup.sh (oh-my-zsh + jimpo theme/plugins,
# editor configs, dotfiles), so the box stays DRY with the host environment.
# `tree-sitter-cli` is a dependency because the nvim config's treesitter
# plugin builds parsers with it.
#
# In BOTH playbooks on purpose, and the reason `runas` scoping exists: the
# box runs the admin half once (the privileged user needs a usable shell for
# `devbox box ssh`, the debugging entry point), and each attaching workspace runs
# the agent half in its own account. That means one clone and one oh-my-zsh
# install per account - a few minutes per attach, accepted deliberately. The apt
# half lives in the `dotfiles-packages` mod, which runs once per box, as root.
#
# Idempotency: oh-my-zsh is a no-op when already present; the devbox zsh env
# block is appended once. (setup.sh's own steps must be re-run-safe - a configenv
# concern; it owns ~/.zshrc and leaves ~/.zshenv alone, which is what makes the
# env block below safe to append there.)
set -euo pipefail

CFG="$(mktemp -d)"
git clone --depth 1 https://github.com/jimpo/configenv "$CFG"
cd "$CFG"

# setup.sh as the invoking user: oh-my-zsh + theme/plugins, editor configs,
# dotfiles. -f = force-overwrite (non-interactive; no cp clobber prompts).
sh setup.sh -f

# devbox-specific zsh env (configenv doesn't set this): put jj / claude
# (~/.local/bin) on PATH. In ~/.zshenv, which EVERY zsh reads - a human at a
# shell, and `ssh <box> '<cmd>'`, which runs the login shell non-interactively
# and never reads ~/.zshrc. This is the zsh half of what ~/.bash_env (the
# BASH_ENV target, written by the provisioning driver) does for bash; ~/.zshenv
# is also where run_envvars wires the [env] creds. Appended once. A Rust
# toolchain puts ~/.cargo on PATH itself.
if ! grep -q 'devbox common env' "$HOME/.zshenv" 2>/dev/null; then
  cat >> "$HOME/.zshenv" <<'ZENV'

# --- devbox common env ---
export PATH="$HOME/.local/bin:$PATH"
ZENV
fi

rm -rf "$CFG"
