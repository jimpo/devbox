#!/usr/bin/env bash
#
# mod: dotfiles - full shell/editor/tool setup from github.com/jimpo/configenv,
# delegating to its own install.sh (apt packages) and setup.sh (oh-my-zsh +
# jimpo theme/plugins, editor configs, dotfiles) so the box stays DRY with the
# host environment.
#
# Idempotency: apt install and oh-my-zsh are no-ops when already present; the
# devbox zsh env block is appended once. (setup.sh's own steps must be
# re-run-safe - a configenv concern.)
set -euo pipefail

CFG="$(mktemp -d)"
git clone --depth 1 https://github.com/jimpo/configenv "$CFG"
cd "$CFG"

# install.sh apt-installs the package set (incl. unzip, which setup.sh's vim
# CSApprox step needs). Run under sudo (the dev user has NOPASSWD), per
# configenv's README; DEBIAN_FRONTEND keeps any debconf prompt from blocking.
sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive sh install.sh

# setup.sh (as the dev user): oh-my-zsh + theme/plugins, editor configs,
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
