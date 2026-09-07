#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# ///
#
# mod: dotfiles-packages - configenv's install.sh, which apt-installs the
# package set the shared configs expect system-wide (incl. unzip, which the
# vim CSApprox step needs).
#
# BOX-LEVEL, and root: apt is the machine's, so this runs once per box rather
# than once per attachment. It cannot be expressed as `packages` either -
# install.sh is an arbitrary script from a clone, not a package list. The
# per-account half of configenv is the `dotfiles` module, which places the
# files themselves out of the nix store and needs no privilege.
#
# Idempotency: apt install is a no-op when the packages are already present, so
# a reconverge is fast.
set -euo pipefail

CFG="$(mktemp -d)"
git clone --depth 1 https://github.com/jimpo/configenv "$CFG"
cd "$CFG"

# Already root (runas: root), so no sudo here - a mod body has none anyway.
# DEBIAN_FRONTEND keeps any debconf prompt from blocking.
apt-get update -qq
DEBIAN_FRONTEND=noninteractive sh install.sh

rm -rf "$CFG"
