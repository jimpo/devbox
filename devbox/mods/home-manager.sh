#!/usr/bin/env bash
#
# /// mod
# requires = ["nix"]
# ///
#
# mod: home-manager - switch this account to the generation the payload's flake
# describes. The agent's declarative half: everything a converted mod used to
# install imperatively is a `<name>.nix` beside the scripts instead, which
# devbox assembles into the payload's flake, and one `home-manager switch`
# reconciles the whole set.
#
# AGENT-SCOPED (the default runas), deliberately. The box's privileged account
# is not managed by home-manager: it is the machine's debugging entry point
# (`devbox box ssh`) and keeps the apt zsh/vim the init script installed, so it
# works whatever state an attachment's generation is in. `nix` itself is the
# box mod that owns /nix and the daemon; this mod only uses them, which is what
# `requires = ["nix"]` says - satisfied across the playbook boundary, since the
# box playbook always runs first.
#
# THE FLAKE IS THE PAYLOAD'S. `$DEVBOX_PAYLOAD_DIR/nix` is unpacked per run and
# deleted afterwards, so there is no copy on the box to edit; the way to
# re-switch is `devbox sync --mods=home-manager`, which ships it again. It is
# named with a `path:` prefix because a flake inside a git work tree ignores
# untracked files and the payload is not a repo - a bare path would silently
# evaluate the wrong thing on a box where /tmp happened to sit in one. The
# directory belongs to the privileged account and this mod only reads it, so
# the switch writes nothing back into it (see --no-write-lock-file below);
# everything it produces lands in the store, in home-manager's own temp
# directory, and under $HOME.
#
# THE SYSTEM IS PART OF THE ATTRIBUTE NAME. devbox cannot know the box's
# architecture from the host, so the flake builds the configuration for
# x86_64-linux and aarch64-linux both, and the name is resolved here, where
# `uname -m` is the answer.
#
# `-b devbox-backup` moves a file the generation wants to own but did not write
# aside to `<file>.devbox-backup`, rather than aborting the switch. An account
# that bash mods have set up has such a file (a plain ~/.zshenv, which
# nix/zshenv.nix owns), and a failed activation would leave the account with no
# generation at all. Only the first switch meets one: after it, every file the
# generation owns is its own symlink and no mod writes there.
#
# Idempotency: `home-manager switch` is a reconcile - an unchanged flake
# produces the same generation and touches nothing.
#
# Context (from the environment): DEVBOX_PAYLOAD_DIR.
set -euo pipefail

# The home-manager CLI, pinned to the same rev as the flake's `home-manager`
# input (see nix/flake.nix); bump the two together. Named by its full flake URL
# rather than the `home-manager/...` registry shorthand, so the switch does not
# depend on the global flake registry resolving.
HM_REV="65258d5c65a250189fde2e35f490d15e064c4c62"
HM_FLAKE="github:nix-community/home-manager/$HM_REV"

flake="${DEVBOX_PAYLOAD_DIR:?home-manager mod: DEVBOX_PAYLOAD_DIR is not set}/nix"
[ -f "$flake/flake.nix" ] || {
  echo "home-manager: the payload has no flake at $flake" >&2
  exit 1
}

# The multi-user install puts nix here. ~/.bash_env sources the installer's
# /etc/profile.d script, which normally has done this already; fall back so
# this mod does not depend on that having happened.
if ! command -v nix >/dev/null 2>&1; then
  export PATH="/nix/var/nix/profiles/default/bin:$PATH"
fi
command -v nix >/dev/null 2>&1 || {
  echo "home-manager: nix is not on PATH and /nix/var/nix/profiles/default/bin" >&2
  echo "  has no nix either - the box's \`nix\` mod has not run." >&2
  exit 1
}

case "$(uname -m)" in
  x86_64)          system="x86_64-linux" ;;
  aarch64 | arm64) system="aarch64-linux" ;;
  *) echo "home-manager mod: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

# `--no-write-lock-file` is safe because both of the flake's inputs are pinned
# to an exact rev (see nix/flake.nix): resolution is already deterministic, so a
# lock file would record only what the pins already say. It sits AFTER the `--`
# because it is home-manager's flag, not `nix run`'s - home-manager recognizes
# it by name and passes it through to the `nix build` that evaluates the
# `--flake` argument. Before the `--` it would apply to resolving $HM_FLAKE,
# which is a different flake with a lock of its own.
echo "==> home-manager switch (agent-$system)"
nix run "$HM_FLAKE" -- switch -b devbox-backup --no-write-lock-file \
  --flake "path:$flake#agent-$system"
