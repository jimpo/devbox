#!/usr/bin/env bash
#
# mod: claude-code - Claude Code (native installer -> ~/.local/bin/claude) plus
# the github/linear plugins common to every project.
#
# Idempotency: the installer overwrites in place; `marketplace add` /
# `plugin install` abort under `set -e` when the marketplace or a plugin is
# already present (every reconverge), so `|| true` tolerates that. A genuine
# first-run failure still surfaces in the tee'd log.
set -euo pipefail

curl -fsSL https://claude.ai/install.sh | bash

claude plugin marketplace add anthropics/claude-plugins-official || true

# Auto-approve every tool call in the sandbox: it's a disposable, isolated VM,
# so the interactive permission prompt is pure friction. `bypassPermissions` is
# the permission mode that skips all approval prompts. Merged into any existing
# settings.json (never clobbering other keys) with jq so it's reconverge-safe.
sudo apt-get install -y jq
settings="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"
[ -f "$settings" ] || echo '{}' >"$settings"
tmp="$(mktemp)"
jq '.permissions.defaultMode = "bypassPermissions"' "$settings" >"$tmp" && mv "$tmp" "$settings"

