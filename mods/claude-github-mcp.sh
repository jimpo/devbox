#!/usr/bin/env bash
#
# requires: claude-code

claude mcp remove --scope user github 2>/dev/null || true
claude mcp add --scope user --transport http github https://api.githubcopilot.com/mcp/ --header 'Authorization: Bearer ${GITHUB_TOKEN}'

