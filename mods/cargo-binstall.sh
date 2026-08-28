#!/usr/bin/env bash
#
# mod: cargo-binstall - a pinned prebuilt cargo-binstall release, for installing
# Rust CLI tools from prebuilt binaries instead of compiling them. Bump
# BINSTALL_VERSION by hand; see
# https://github.com/cargo-bins/cargo-binstall/releases .
#
# NO RUST TOOLCHAIN NEEDED: the release is a statically linked musl binary, and
# it resolves and fetches crates entirely on its own - verified against a host
# with no cargo/rustc at all. That is what makes it worth having in the base
# playbook rather than in a workspace's rust mod: `cargo-binstall ripgrep` works
# on a box that has never seen rustup. Invoke it as `cargo-binstall` (the
# `cargo binstall` subcommand form needs cargo, which the base box has not got).
#
# Idempotency: skips the download once the pinned version is installed; the
# ~/.cargo/env write below is guarded on the file's absence, and the ~/.zshenv
# source line on its own presence.
set -euo pipefail

BINSTALL_VERSION="1.21.1"
if ! cargo-binstall -V 2>/dev/null | grep -qF "$BINSTALL_VERSION"; then
  # Pick the release asset for this machine's architecture (devbox runs on both
  # x86_64 libvirt/EC2 and aarch64 EC2). Both ship musl builds.
  case "$(uname -m)" in
    x86_64)          binstall_arch="x86_64" ;;
    aarch64 | arm64) binstall_arch="aarch64" ;;
    *) echo "cargo-binstall mod: unsupported architecture $(uname -m)" >&2; exit 1 ;;
  esac
  curl -fsSL -o /tmp/cargo-binstall.tgz \
    "https://github.com/cargo-bins/cargo-binstall/releases/download/v${BINSTALL_VERSION}/cargo-binstall-${binstall_arch}-unknown-linux-musl.tgz"
  tar -xzf /tmp/cargo-binstall.tgz -C /tmp cargo-binstall
  install -Dm755 /tmp/cargo-binstall "$HOME/.local/bin/cargo-binstall"
  rm -f /tmp/cargo-binstall.tgz /tmp/cargo-binstall
fi

# cargo-binstall itself lands in ~/.local/bin, which devbox already puts on PATH
# - but the crates it installs go to ~/.cargo/bin, which is on PATH only via
# ~/.cargo/env (see the driver's install_bash_env). That file is rustup's, and
# on a box with no rust toolchain nothing creates it, so every tool installed
# through binstall would be invisible. Write the equivalent snippet when it is
# absent; rustup overwrites it with its own (equivalent) version if a workspace's
# rust mod later runs, so the two cannot conflict.
if [ ! -f "$HOME/.cargo/env" ]; then
  mkdir -p "$HOME/.cargo"
  cat >"$HOME/.cargo/env" <<'CARGOENV'
#!/bin/sh
# devbox: put ~/.cargo/bin (where cargo-binstall installs) on PATH. Replaced by
# rustup's own version of this file if a rust toolchain is ever installed.
case ":${PATH}:" in
    *:"$HOME/.cargo/bin":*) ;;
    *) export PATH="$HOME/.cargo/bin:$PATH" ;;
esac
CARGOENV
fi

# ~/.cargo/env is on the path of non-interactive bash through ~/.bash_env (the
# BASH_ENV target, written by the driver). zsh reads neither, so source it from
# ~/.zshenv too - the file EVERY zsh reads, so ~/.cargo/bin reaches a human at a
# shell and `ssh <box> '<cmd>'` alike. Whoever owns ~/.cargo/env - this mod's
# stub or rustup's - the line is the same, so the wiring belongs here, next to
# the file it sources.
CARGO_ENV_LINE='[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"'
touch "$HOME/.zshenv"
if ! grep -qxF "$CARGO_ENV_LINE" "$HOME/.zshenv"; then
  printf '%s\n' "$CARGO_ENV_LINE" >>"$HOME/.zshenv"
fi
