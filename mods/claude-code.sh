#!/usr/bin/env bash
#
# /// mod
# packages = ["jq"]
# ///
#
# mod: claude-code - Claude Code (native installer -> ~/.local/bin/claude) plus
# the github/linear plugins common to every workspace.
#
# Runs as the agent (the default target): this is the account a workspace's agents
# work in, and the only one that needs Claude Code at all. Each attached workspace
# gets its own install, in its own home.
#
# Idempotency: the installer overwrites in place; `marketplace add` /
# `plugin install` abort under `set -e` when the marketplace or a plugin is
# already present (every reconverge), so `|| true` tolerates that. A genuine
# first-run failure still surfaces in the tee'd log.
set -euo pipefail

curl -fsSL https://claude.ai/install.sh | bash

claude plugin marketplace add anthropics/claude-plugins-official || true

# Auto-approve every tool call in the sandbox: it's a disposable, isolated VM
# and the agent runs as an account with no sudo, so the interactive permission
# prompt is pure friction. `bypassPermissions` is the permission mode that skips
# all approval prompts. Merged into any existing settings.json (never clobbering
# other keys) with jq (see the `packages` metadata above) so it's reconverge-safe.
settings="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"
[ -f "$settings" ] || echo '{}' >"$settings"
tmp="$(mktemp)"
jq '.permissions.defaultMode = "bypassPermissions"' "$settings" >"$tmp" && mv "$tmp" "$settings"

