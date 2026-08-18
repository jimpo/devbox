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
# `devbox box ssh`, the debugging entry point), and each attaching project runs
# the agent half in its own account. That means one clone and one oh-my-zsh
# install per account - a few minutes per attach, accepted deliberately. The apt
# half lives in the `dotfiles-packages` mod, which runs once per box, as root.
#
# Idempotency: oh-my-zsh is a no-op when already present; the devbox zsh env
# block is appended once. (setup.sh's own steps must be re-run-safe - a configenv
# concern.)
set -euo pipefail

CFG="$(mktemp -d)"
git clone --depth 1 https://github.com/jimpo/configenv "$CFG"
cd "$CFG"

# setup.sh as the invoking user: oh-my-zsh + theme/plugins, editor configs,
# dotfiles. -f = force-overwrite (non-interactive; no cp clobber prompts).
sh setup.sh -f

# devbox-specific interactive-zsh env (configenv doesn't set this): put jj /
# claude (~/.local/bin) and cargo on PATH for login shells. Appended once.
if ! grep -q 'devbox common env' "$HOME/.zshrc" 2>/dev/null; then
  cat >> "$HOME/.zshrc" <<'ZRC'

# --- devbox common env ---
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
export PATH="$HOME/.local/bin:$PATH"
ZRC
fi

rm -rf "$CFG"
