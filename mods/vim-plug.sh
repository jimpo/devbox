#!/usr/bin/env bash
#
# /// mod
# requires = ["dotfiles"]
# runas = ["admin"]
# ///
#
# mod: vim-plug - vim-plug plus the plugins the privileged account's ~/.vimrc
# declares (base16-vim / vim-go / rust.vim). Depends on the dotfiles mod having
# written that ~/.vimrc, so it is ordered after it in the box playbook.
#
# THE BOX'S HALF OF VIM, and only the box's. The privileged account is the
# machine's debugging entry point (`devbox box ssh`) and has no home-manager
# generation, so it keeps its apt vim and fetches its plugins here; an agent's
# vim is the flake's `nix/modules/vim.nix`, which takes the same three plugins
# from the store. The two halves carry DIFFERENT NAMES because devbox resolves
# one name to one implementation: a `vim.sh` in any mods directory would shadow
# the module and leave every agent without plugins.
#
# The .vimrc dotfiles installs calls plug#begin(), so an account with the
# dotfiles and no plug.vim gets an error on every vim start - which is what
# makes this the box's mod rather than an optional one.
#
# Idempotency: plug.vim is fetched unconditionally; PlugInstall is a no-op for
# already-installed plugins.
set -euo pipefail

curl -fLo "$HOME/.vim/autoload/plug.vim" --create-dirs \
  https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim
# The install happens whatever the exit status: on a first run the .vimrc's
# `colorscheme base16-chalk` fails until this very command has fetched
# base16-vim, and vim in ex mode reports that as a failure afterwards.
vim -E -s -u "$HOME/.vimrc" +PlugInstall +qall || true
