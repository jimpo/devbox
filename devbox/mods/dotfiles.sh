#!/usr/bin/env bash
#
# mod: dotfiles - gitconfig + vimrc + oh-my-zsh (the `jimpo` theme only; NOT the
# other custom zsh settings) from github.com/jimpo/configenv.
#
# Idempotency: the copied dotfiles are overwritten unconditionally; the oh-my-zsh
# install is guarded on its directory; the ~/.zshrc "devbox common env" block is
# appended only once.
set -euo pipefail

CFG="$(mktemp -d)"
git clone --depth 1 https://github.com/jimpo/configenv "$CFG"
cp "$CFG/gitconfig" "$HOME/.gitconfig"
cp "$CFG/vimrc" "$HOME/.vimrc"

if [ ! -d "$HOME/.oh-my-zsh" ]; then
  RUNZSH=no CHSH=no KEEP_ZSHRC=no \
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"
fi
mkdir -p "$HOME/.oh-my-zsh/custom/themes"
cp "$CFG/oh-my-zsh/custom/themes/jimpo.zsh-theme" "$HOME/.oh-my-zsh/custom/themes/"
sed -i 's/^ZSH_THEME=.*/ZSH_THEME="jimpo"/' "$HOME/.zshrc"
# The jimpo theme's RPS1 renders a vi INS/CMD indicator that needs the vi-mode
# plugin's KEYMAP - enable it (but not the other custom plugins).
sed -i 's/^plugins=.*/plugins=(git vi-mode)/' "$HOME/.zshrc"

if ! grep -q 'devbox common env' "$HOME/.zshrc"; then
  cat >> "$HOME/.zshrc" <<'ZRC'

# --- devbox common env ---
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
export PATH="$HOME/.local/bin:$PATH"
ZRC
fi
rm -rf "$CFG"
