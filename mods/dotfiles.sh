#!/usr/bin/env bash
#
# /// mod
# requires = ["dotfiles-packages"]
# runas = ["agent"]
# ///
#
# mod: dotfiles - the per-user half of the configenv setup from
# github.com/jimpo/configenv: its setup.sh (oh-my-zsh + jimpo theme/plugins,
# editor configs, dotfiles), so the box stays DRY with the host environment.
# The nvim config it copies is read by the `nvim` an attachment's home-manager
# generation provides, which also carries the tree-sitter CLI that config
# builds parsers with.
#
# AGENT-ONLY: the privileged account keeps stock apt bash and unconfigured
# vim, deliberately independent of any per-account setup that could itself
# need fixing (see `flake.nix`'s "home-manager is agent-only" note - the same
# reasoning extends to configenv). The apt half lives in the
# `dotfiles-packages` mod, which runs once per box, as root.
#
# CONFIGENV OWNS ~/.zshrc, and that is load-bearing: an agent account's
# ~/.zshenv belongs to its home-manager generation (nix/zshenv.nix), and the two
# stay out of each other's files. setup.sh writes ~/.zshrc (through oh-my-zsh),
# ~/.vimrc, ~/.tmux.conf, ~/.gitconfig and ~/.config/{jj,nvim,base16-shell}, and
# never ~/.zshenv.
#
# Idempotency: oh-my-zsh is a no-op when already present. (setup.sh's own
# steps must be re-run-safe - a configenv concern.)
set -euo pipefail

CFG="$(mktemp -d)"
git clone --depth 1 https://github.com/jimpo/configenv "$CFG"
cd "$CFG"

# setup.sh as the invoking user: oh-my-zsh + theme/plugins, editor configs,
# dotfiles. -f = force-overwrite (non-interactive; no cp clobber prompts).
sh setup.sh -f

rm -rf "$CFG"
