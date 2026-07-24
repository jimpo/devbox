#!/usr/bin/env bash
#
# mod: vim - vim-plug plus the plugins declared in ~/.vimrc (base16-vim /
# vim-go / rust.vim). Depends on the dotfiles mod having written ~/.vimrc, so it
# is ordered after it in the base playbook.
#
# requires: dotfiles
#
# Idempotency: plug.vim is fetched unconditionally; PlugInstall is a no-op for
# already-installed plugins.
set -euo pipefail

curl -fLo "$HOME/.vim/autoload/plug.vim" --create-dirs \
  https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim
vim -E -s -u "$HOME/.vimrc" +PlugInstall +qall || true
