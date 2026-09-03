#!/usr/bin/env bash
#
# /// mod
# requires = ["claude-code"]
# ///
#
# mod: claude-linear-mcp - Linear's MCP server, registered with the agent's
# Claude Code. Opt-in: a workspace names `claude-linear-mcp` in its own `mods`.
# It stays a separate name from `claude-github-mcp` because a workspace picks
# its servers one at a time.
#
# THE TOKEN IS NEVER RESOLVED HERE. The header carries the literal, unexpanded
# `${LINEAR_API_KEY}` - single-quoted, so this shell leaves it alone - and
# Claude Code resolves it from the environment when it calls the server. That
# environment gets it from the 0600 ~/.config/devbox/env the account's ~/.zshenv
# sources, so the value is in one 0600 file and in no config this mod writes.
#
# `mcp add` on a name that is already registered is an error, so the remove
# first is what makes a reconverge work and what lets the URL or the header
# change.
set -euo pipefail

claude mcp remove --scope user linear 2>/dev/null || true

# SC2016 is the point: the header is stored literally, and Claude Code expands
# it from the environment when it calls the server.
# shellcheck disable=SC2016
claude mcp add --scope user --transport http linear https://mcp.linear.app/mcp --header 'Authorization: Bearer ${LINEAR_API_KEY}'
