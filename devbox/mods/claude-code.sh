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

