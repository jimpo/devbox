#!/usr/bin/env bash
#
# /// mod
# requires = ["claude-code"]
# ///
#
# mod: claude-github-mcp - GitHub's MCP server, registered with the agent's
# Claude Code. Opt-in: a workspace names `claude-github-mcp` in its own `mods`.
#
# THE TOKEN IS NEVER RESOLVED HERE. The header carries the literal, unexpanded
# `${GITHUB_TOKEN}` - single-quoted, so this shell leaves it alone - and Claude
# Code resolves it from the environment when it calls the server. That
# environment gets it from the 0600 ~/.config/devbox/env the account's ~/.zshenv
# sources, so the value is in one 0600 file and in no config this mod writes.
#
# `mcp add` on a name that is already registered is an error, so the remove
# first is what makes a reconverge work and what lets the URL or the header
# change.
set -euo pipefail

claude mcp remove --scope user github 2>/dev/null || true

# SC2016 is the point: the header is stored literally, and Claude Code expands
# it from the environment when it calls the server.
# shellcheck disable=SC2016
claude mcp add --scope user --transport http github https://api.githubcopilot.com/mcp/ --header 'Authorization: Bearer ${GITHUB_TOKEN}'
