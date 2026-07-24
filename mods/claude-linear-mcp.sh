#!/usr/bin/env bash
#
# requires: claude-code

claude mcp remove --scope user linear 2>/dev/null || true
claude mcp add --scope user --transport http linear https://mcp.linear.app/mcp --header 'Authorization: Bearer ${LINEAR_API_KEY}'

