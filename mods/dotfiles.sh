#!/usr/bin/env bash
#
# /// mod
# requires = ["dotfiles-packages"]
# runas = ["admin", "agent"]
# ///
#
# mod: dotfiles - the per-user half of the configenv setup from
# github.com/jimpo/configenv: its setup.sh (oh-my-zsh + jimpo theme/plugins,
# editor configs, dotfiles), so the box stays DRY with the host environment.
# The nvim config it copies is read by the `nvim` an attachment's home-manager
# generation provides, which also carries the tree-sitter CLI that config
# builds parsers with.
#
# In BOTH playbooks on purpose, and the reason `runas` scoping exists: the
# box runs the admin half once (the privileged user needs a usable shell for
# `devbox box ssh`, the debugging entry point), and each attaching workspace runs
# the agent half in its own account. That means one clone and one oh-my-zsh
# install per account - a few minutes per attach, accepted deliberately. The apt
# half lives in the `dotfiles-packages` mod, which runs once per box, as root.
#
# CONFIGENV OWNS ~/.zshrc, and that is load-bearing: an agent account's
# ~/.zshenv belongs to its home-manager generation (nix/zshenv.nix), and the two
# stay out of each other's files. setup.sh writes ~/.zshrc (through oh-my-zsh),
# ~/.vimrc, ~/.tmux.conf, ~/.gitconfig and ~/.config/{jj,nvim,base16-shell}, and
# never ~/.zshenv.
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

# devbox-specific zsh env (configenv doesn't set this): put ~/.local/bin on
# PATH. In ~/.zshenv, which EVERY zsh reads - a human at a shell, and
# `ssh <box> '<cmd>'`, which runs the login shell non-interactively and never
# reads ~/.zshrc. This is the zsh half of what ~/.bash_env (the BASH_ENV target,
# written by the provisioning driver) does for bash. Appended once.
#
# THE PRIVILEGED ACCOUNT ONLY, which is the account this mod's box half runs in:
# it is not managed by home-manager, so nothing else writes its ~/.zshenv. In an
# agent account that file is a read-only symlink into the store, written by the
# generation (nix/zshenv.nix) with this same directory on PATH and that
# account's [env] creds sourced; appending to it would replace the symlink and
# the next activation would refuse a file it does not own.
if [ "$(id -un)" = "${DEVBOX_ADMIN_USER:-}" ] &&
   ! grep -q 'devbox common env' "$HOME/.zshenv" 2>/dev/null; then
  cat >> "$HOME/.zshenv" <<'ZENV'

# --- devbox common env ---
export PATH="$HOME/.local/bin:$PATH"
ZENV
fi

rm -rf "$CFG"
