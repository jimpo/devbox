#!/usr/bin/env bash
#
# Shared, re-runnable post-boot provisioning for every devbox-managed VM.
# Ships with devbox (devbox.py resolves it via Path(__file__).parent). It runs
# two ways, over the SAME rendered script:
#
#   * first boot  - cloud-init writes this to /usr/local/bin/provision-common.sh
#                   (devbox injects it into the NoCloud seed's write_files) and
#                   runs it once as %DEVBOX_USER% (see common-cloud-config.yaml).
#   * reconverge  - `devbox provision` pipes it over SSH and re-runs it against
#                   an already-booted box, so adding a package / bumping jj /
#                   adding a Claude plugin no longer needs destroy+create.
#
# It must therefore be idempotent: every step either overwrites unconditionally
# or guards on "already done". A future AWS provider renders and runs this same
# script through its own first-boot mechanism.
#
# %DEVBOX_USER%, %DEVBOX_JJ_NAME%, %DEVBOX_JJ_EMAIL%, %DEVBOX_GITHUB_TOKEN%,
# %DEVBOX_REPO_MODE%, %DEVBOX_REPO_DIR%, %DEVBOX_REPO_URL% are substituted by
# literal %TOKEN% replacement in devbox.py (render_provision_script) before the
# script is transferred - it is NOT a cloud-init/jinja template.
set -euxo pipefail

cat > "$HOME/.bash_env" <<'BENV'
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
BENV
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"

# ---------------------------------------------------------------------
# jj (jujutsu) - a pinned prebuilt release, independent of any project's
# own language toolchain (jj identity + repo bootstrap below must work for
# every project, including non-Rust ones). Bump by hand; see
# https://github.com/jj-vcs/jj/releases . Skipped on a reconverge run once
# the pinned version is already installed.
# ---------------------------------------------------------------------
JJ_VERSION="0.43.0"
if ! jj --version 2>/dev/null | grep -qF "$JJ_VERSION"; then
  curl -fsSL -o /tmp/jj.tar.gz \
    "https://github.com/jj-vcs/jj/releases/download/v${JJ_VERSION}/jj-v${JJ_VERSION}-x86_64-unknown-linux-musl.tar.gz"
  tar -xzf /tmp/jj.tar.gz -C /tmp ./jj
  install -Dm755 /tmp/jj "$HOME/.local/bin/jj"
  rm -f /tmp/jj.tar.gz /tmp/jj
fi

jj config set --user user.name "%DEVBOX_JJ_NAME%"
jj config set --user user.email "%DEVBOX_JJ_EMAIL%"

# ---------------------------------------------------------------------
# Claude Code (native installer -> ~/.local/bin/claude) + plugins common
# to every project. A project's own cloud-config.yaml can install more
# plugins from its own runcmd, which always runs after this script (see
# devbox's README for the merge/ordering rules).
# ---------------------------------------------------------------------
curl -fsSL https://claude.ai/install.sh | bash
# `marketplace add` / `plugin install` abort under `set -e` when the
# marketplace or a plugin is already present, which every reconverge run
# hits - tolerate that so re-runs succeed. A genuine first-run failure
# still shows up in the tee'd provision.log via the `set -x` trace.
claude plugin marketplace add anthropics/claude-plugins-official || true
claude plugin install github || true
claude plugin install linear || true

# ---------------------------------------------------------------------
# Dotfiles from github.com/jimpo/configenv
# (gitconfig + vimrc + oh-my-zsh `jimpo` theme only; NOT the other
#  custom zsh settings: aliases.zsh / settings.zsh / direnv.zsh)
# ---------------------------------------------------------------------
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
# The jimpo theme's RPS1 renders a vi INS/CMD indicator that needs the
# vi-mode plugin's KEYMAP - enable it (but not the other custom plugins).
sed -i 's/^plugins=.*/plugins=(git vi-mode)/' "$HOME/.zshrc"

if ! grep -q 'devbox common env' "$HOME/.zshrc"; then
  cat >> "$HOME/.zshrc" <<'ZRC'

# --- devbox common env ---
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
export PATH="$HOME/.local/bin:$PATH"
ZRC
fi
rm -rf "$CFG"

# ---------------------------------------------------------------------
# vim plugins (vim-plug): base16-vim / vim-go / rust.vim
# ---------------------------------------------------------------------
curl -fLo "$HOME/.vim/autoload/plug.vim" --create-dirs \
  https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim
vim -E -s -u "$HOME/.vimrc" +PlugInstall +qall || true

# ---------------------------------------------------------------------
# GitHub auth (best-effort): wires up `gh` and git/jj HTTPS auth for
# github.com if [github].token (a fine-grained PAT) was set in devbox's
# global config. Skipped entirely if unset - cloning a public repo works
# fine unauthenticated. Runs before the repo bootstrap below so a "clone"
# of a private repo can succeed. `set +x` around it keeps the PAT out of
# the tee'd provision.log.
# ---------------------------------------------------------------------
set +x
if [ -n "%DEVBOX_GITHUB_TOKEN%" ]; then
  echo "%DEVBOX_GITHUB_TOKEN%" | gh auth login --with-token
  gh auth setup-git
fi
set -x

# ---------------------------------------------------------------------
# Repo bootstrap (.devbox/config.toml [repo]): "clone" pulls an existing
# repo via jj; "push" leaves an empty colocated jj+git repo ready to
# receive `jj git push --remote sandbox -b main` from the host; "none"
# skips this entirely. Guarded on the target dir so reconverge is a no-op.
# ---------------------------------------------------------------------
case "%DEVBOX_REPO_MODE%" in
  clone)
    if [ ! -d "$HOME/%DEVBOX_REPO_DIR%" ]; then
      jj git clone "%DEVBOX_REPO_URL%" "$HOME/%DEVBOX_REPO_DIR%"
    fi
    ;;
  push)
    if [ ! -d "$HOME/%DEVBOX_REPO_DIR%" ]; then
      git init --initial-branch=main "$HOME/%DEVBOX_REPO_DIR%"
      (cd "$HOME/%DEVBOX_REPO_DIR%" \
        && jj git init --colocate \
        && git config receive.denyCurrentBranch updateInstead)
    fi
    ;;
  none) ;;
  *)
    echo "unknown DEVBOX_REPO_MODE: %DEVBOX_REPO_MODE%" >&2
    exit 1
    ;;
esac

echo "common provisioning complete"
