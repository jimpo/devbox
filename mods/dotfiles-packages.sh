#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# ///
#
# mod: dotfiles-packages - the root half of the configenv setup: its install.sh,
# which apt-installs the package set the dotfiles/editor config expects (incl.
# unzip, which setup.sh's vim CSApprox step needs).
#
# Split out of the `dotfiles` mod because it is the one piece that genuinely
# needs privilege and cannot be expressed as `packages` - install.sh is an
# arbitrary script from a clone, not a package list. Being system-wide, it is
# also a genuinely box-level mod in the base: it runs once per box, while its
# partner runs setup.sh per account, unprivileged.
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
