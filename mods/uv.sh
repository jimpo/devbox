#!/usr/bin/env bash
#
# mod: uv - the Python package/tool manager (installs to ~/.local/bin). Used
# both for the foregent checkout itself and to install CAO as a uv tool, so it
# is ordered before the cao mod.
#
# Idempotency: the installer overwrites in place.
set -euo pipefail

curl -LsSf https://astral.sh/uv/install.sh | sh
