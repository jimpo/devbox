#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# ///
#
# mod: cloudflared - Cloudflare's `cloudflared` binary, from their apt repo.
# Installs it and nothing else; `cloudflared-tunnel` is the mod that runs a
# tunnel, once per attached workspace, in that workspace's own account.
#
# Root because it adds an apt repo and installs from it. The package cannot be
# a `packages` entry: it lives in Cloudflare's own repo, which does not exist
# until this mod adds it.
#
# Idempotency: the keyring/repo files and the apt install overwrite/reconcile
# unconditionally.
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
