#!/usr/bin/env bash
#
# /// mod
# packages = ["jq"]
# ///
#
# mod: claude-code - Claude Code in the agent's home, from the native installer,
# plus the plugin marketplace every workspace gets.
#
# THE INSTALLER'S BINARY KEEPS ITSELF CURRENT, which is the reason this is a
# script and not a package in the generation. `claude.ai/install.sh` puts
# ~/.local/bin/claude in the account's own home and leaves the auto-updater on,
# so a long-lived box follows Claude Code's releases by itself, between
# reconverges. A store path is read-only and could only ever hold the version
# its nixpkgs pin names.
#
# Nothing in the generation provides a `claude`, so the account's PATH order
# (~/.nix-profile/bin ahead of ~/.local/bin) leaves this the only one, and
# `devbox sync --mods` re-runs it.
#
# Runs as the agent (the default target): this is the account a workspace's
# agents work in, and the only one that needs Claude Code at all. Each attached
# workspace gets its own install, in its own home.
#
# Idempotency: the installer overwrites in place; `marketplace add` exits
# non-zero once the marketplace is already registered, which is every
# reconverge, so `|| true` tolerates that. A genuine first-run failure still
# surfaces in the tee'd log.
set -euo pipefail

curl -fsSL https://claude.ai/install.sh | bash

claude plugin marketplace add anthropics/claude-plugins-official || true

# Auto-approve every tool call in the sandbox: it's a disposable, isolated VM
# and the agent runs as an account with no sudo, so the interactive permission
# prompt is pure friction. `bypassPermissions` is the permission mode that skips
# all approval prompts. Merged into any existing settings.json (never clobbering
# other keys) with jq (see the `packages` metadata above) so it's reconverge-safe
# and leaves what `/config` wrote in a live session alone.
settings="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"
[ -f "$settings" ] || echo '{}' >"$settings"
tmp="$(mktemp)"
jq '.permissions.defaultMode = "bypassPermissions"' "$settings" >"$tmp" && mv "$tmp" "$settings"
