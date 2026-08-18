#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# packages = ["unzip"]
# ///
#
# mod: tree-sitter-cli - a pinned prebuilt `tree-sitter` release, unzipped
# straight to /usr/local/bin. Bump TREE_SITTER_VERSION by hand; see
# https://github.com/tree-sitter/tree-sitter/releases .
#
# Root because it writes under /usr/local, one system-wide install for both
# accounts - and a zipped binary is not something `packages` can express.
#
# Idempotency: skips the download once the pinned version is installed.
set -euo pipefail

TREE_SITTER_VERSION="0.26.12"
if ! tree-sitter --version 2>/dev/null | grep -qF "$TREE_SITTER_VERSION"; then
  # Pick the release asset for this machine's architecture (devbox runs on both
  # x86_64 libvirt/EC2 and aarch64 EC2).
  case "$(uname -m)" in
    x86_64)          ts_arch="x64" ;;
    aarch64 | arm64) ts_arch="arm64" ;;
    *) echo "tree-sitter-cli mod: unsupported architecture $(uname -m)" >&2; exit 1 ;;
  esac
  curl -fsSL -o /tmp/tree-sitter-cli.zip \
    "https://github.com/tree-sitter/tree-sitter/releases/download/v${TREE_SITTER_VERSION}/tree-sitter-cli-linux-${ts_arch}.zip"
  # The zip holds a single `tree-sitter` binary at its root, no wrapper dir.
  unzip -oq /tmp/tree-sitter-cli.zip -d /usr/local/bin tree-sitter
  chmod 755 /usr/local/bin/tree-sitter
  rm -f /tmp/tree-sitter-cli.zip
fi
