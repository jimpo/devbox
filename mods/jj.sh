#!/usr/bin/env bash
#
# mod: jj - a pinned prebuilt jj (jujutsu) release + the user identity. Kept
# independent of any project's language toolchain: jj identity + the repo
# bootstrap must work for every project, including non-Rust ones. Bump
# JJ_VERSION by hand; see https://github.com/jj-vcs/jj/releases .
#
# Idempotency: skips the download once the pinned version is installed;
# `jj config set` overwrites unconditionally.
#
# Context (from the environment): DEVBOX_JJ_NAME, DEVBOX_JJ_EMAIL.
set -euo pipefail

JJ_VERSION="0.43.0"
if ! jj --version 2>/dev/null | grep -qF "$JJ_VERSION"; then
  # Pick the release asset for this machine's architecture (devbox runs on both
  # x86_64 libvirt/EC2 and aarch64 EC2). jj ships musl builds for both.
  case "$(uname -m)" in
    x86_64)          jj_arch="x86_64" ;;
    aarch64 | arm64) jj_arch="aarch64" ;;
    *) echo "jj mod: unsupported architecture $(uname -m)" >&2; exit 1 ;;
  esac
  curl -fsSL -o /tmp/jj.tar.gz \
    "https://github.com/jj-vcs/jj/releases/download/v${JJ_VERSION}/jj-v${JJ_VERSION}-${jj_arch}-unknown-linux-musl.tar.gz"
  tar -xzf /tmp/jj.tar.gz -C /tmp ./jj
  install -Dm755 /tmp/jj "$HOME/.local/bin/jj"
  rm -f /tmp/jj.tar.gz /tmp/jj
fi

jj config set --user user.name "$DEVBOX_JJ_NAME"
jj config set --user user.email "$DEVBOX_JJ_EMAIL"
