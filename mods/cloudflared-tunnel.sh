#!/usr/bin/env bash
#
# /// mod
# requires = ["cloudflared"]
# ///
#
# mod: cloudflared-tunnel - runs this account's own `cloudflared tunnel run` as
# a detached background process. One tunnel per attachment, so several workspaces
# on one box each hold their own, with their own token. No-op when
# CLOUDFLARE_TOKEN is unset.
#
# Runs as the agent (the default target): the process belongs to the workspace,
# and needs no privilege. `cloudflared` itself does - hence `requires`, which
# also means a workspace has to list BOTH mods, in that order:
#   mods = ["cloudflared", "cloudflared-tunnel"]
#
# The token is this account's own, so a bare string in [env] (owner defaults to
# the agent):
#   CLOUDFLARE_TOKEN = "..."
#
# It reaches cloudflared as TUNNEL_TOKEN in the environment rather than as
# `--token` on the command line, which would show the secret in `ps` to every
# other account on the box.
#
# Idempotency: a tunnel already running for this account is left alone. The
# process does not survive a reboot; the next `devbox sync --mods` starts it
# again.
set -euo pipefail

[ -n "${CLOUDFLARE_TOKEN:-}" ] || exit 0

if pgrep -u "$(id -un)" -f 'cloudflared tunnel' >/dev/null; then
  echo "cloudflared tunnel already running for $(id -un)"
  exit 0
fi

log="$HOME/.local/state/cloudflared.log"
mkdir -p "$(dirname "$log")"

# Detached from this shell AND from its output pipe: the driver tees each mod's
# output, so a child still holding that pipe would keep the provisioning run
# waiting forever. --no-autoupdate (a flag of `tunnel`, hence its place before
# the subcommand) because an update restarts the process, and nothing here is
# watching it.
TUNNEL_TOKEN="$CLOUDFLARE_TOKEN" setsid nohup \
  cloudflared tunnel --no-autoupdate run \
  </dev/null >>"$log" 2>&1 &

echo "cloudflared tunnel started (logging to ${log/#$HOME/\~})"
