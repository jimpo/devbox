#!/usr/bin/env bash
#
# /// mod
# ///
#
# mod: codex - the OpenAI Codex CLI in the agent's home, from OpenAI's own
# standalone installer. Opt-in: a workspace names `codex` in its own `mods`.
#
# SELF-UPDATING, which is why it is the installer and not a package in the
# generation. codex knows how it was installed: for a standalone install
# `codex update` re-runs exactly the command below, and the TUI offers it when a
# newer release exists. ~/.local/bin/codex is a symlink into a release directory
# under ~/.codex, all of it the account's own, so the running binary can replace
# itself.
#
# CODEX_NON_INTERACTIVE=1 because a mod has no terminal: it makes the installer
# skip its prompts, including the offer to start codex when it finishes.
#
# PUTTING ~/.local/bin ON PATH FIRST is what keeps the installer out of the
# account's shell files. It appends an `export PATH=...` block to a shell
# profile only when its install directory is absent from PATH, and the agent's
# ~/.zshenv is a symlink into the read-only store. The generation puts that
# directory on PATH for every later shell (nix/zshenv.nix), so the block would
# be redundant as well as unwritable.
#
# Authentication is the account's: `codex login`, or an OPENAI_API_KEY in the
# workspace's `[env]`, which reaches the shell from the 0600
# ~/.config/devbox/env.
#
# Idempotency: the installer replaces the symlink with whatever release it
# fetched, so a reconverge is an install of the current version.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh

# Names the version in the provisioning log, and fails the mod if the install
# did not produce a working binary.
codex --version
