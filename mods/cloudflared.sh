#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# ///
#
# mod: cloudflared - Cloudflare's `cloudflared` daemon from their apt repo. When
# CLOUDFLARE_TOKEN is set, also installs it as a systemd service running that
# tunnel. No-op service setup when CLOUDFLARE_TOKEN is unset.
#
# Root because it adds an apt repo, installs from it and registers a systemd
# unit. The package cannot be a `packages` entry: it lives in Cloudflare's
# own repo, which does not exist until this mod adds it.
#
# Idempotency: the keyring/repo files and apt install overwrite/reconcile
# unconditionally. The service install is skipped once its unit already exists
# (`cloudflared service install` errors on a second run).
#
# Context (from the environment): CLOUDFLARE_TOKEN, which a root mod sees when
# its [env] entry is the privileged user's:
#   CLOUDFLARE_TOKEN = { value = "...", owner = "admin" }
set -euo pipefail

# Already root (runas: root), so no sudo here - a mod body has none anyway.
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  > /etc/apt/sources.list.d/cloudflared.list

# DPkg::Lock::Timeout matches the driver's own apt calls - a freshly booted box
# can still be holding the dpkg lock in an unattended upgrade.
apt-get -o DPkg::Lock::Timeout=600 update
DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y cloudflared

if [ -n "${CLOUDFLARE_TOKEN:-}" ] && [ ! -f /etc/systemd/system/cloudflared.service ]; then
  cloudflared service install "$CLOUDFLARE_TOKEN"
fi
