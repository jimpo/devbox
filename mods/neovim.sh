#!/usr/bin/env bash
#
# mod: neovim - the current stable Neovim, from the GitHub release tarball
# unpacked into /usr/local (so nvim lands at /usr/local/bin/nvim). `stable` is a
# rolling release pointer, not a pinned version, so this deliberately isn't
# version-checked: each run re-fetches stable and overwrites in place, picking
# up upstream's latest.
#
# runas: root
#
# Root because it writes under /usr/local, one system-wide install for both
# accounts - and an unpacked tarball is not something `# packages:` can express.
#
# Idempotency: the download + extract overwrite the same paths every run.
set -euo pipefail

# Pick the release asset for this machine's architecture (devbox runs on both
# x86_64 libvirt/EC2 and aarch64 EC2).
case "$(uname -m)" in
  x86_64)          nvim_arch="x86_64" ;;
  aarch64 | arm64) nvim_arch="arm64" ;;
  *) echo "neovim mod: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

curl -fsSL -o /tmp/nvim.tar.gz \
  "https://github.com/neovim/neovim/releases/download/stable/nvim-linux-${nvim_arch}.tar.gz"
# --strip-components=1 drops the nvim-linux-<arch>/ wrapper dir so bin/lib/share
# merge straight into /usr/local. Already root (runas: root), so no sudo here.
tar -xzf /tmp/nvim.tar.gz -C /usr/local --strip-components=1
rm -f /tmp/nvim.tar.gz
