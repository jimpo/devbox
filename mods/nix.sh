#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# packages = ["xz-utils"]
# ///
#
# mod: nix - the nix package manager in multi-user (daemon) mode, box-wide.
#
# Multi-user, not single-user: /nix is owned by root and served by nix-daemon,
# so every unprivileged agent account on the box can build and install from it
# without sudo, and they all share one store. A single-user install would put
# the store under whichever account ran it, which the second attached workspace
# could then neither write nor safely read. That is also why this is a box mod:
# the store and the daemon are the machine's, not an attachment's.
#
# THE UPSTREAM INSTALLER, pinned by version. `https://nixos.org/nix/install`
# redirects to a rolling release, so the pinned `releases.nixos.org/nix/nix-X.Y.Z`
# URL is what makes two boxes created a month apart get the same nix - the same
# reason the `jj` mod pins its release. The pinned script carries the SHA-256 of
# the binary tarball it downloads and checks it, so nothing here has to. It is
# the installer nix itself ships, and this box is plain Ubuntu with systemd,
# which is that installer's primary target: none of the extra ground a
# third-party installer covers (WSL, SELinux, no-systemd, macOS) applies here.
# Bump NIX_VERSION by hand; see https://nixos.org/download/ .
#
# `--yes` is what makes it unattended: the driver runs over SSH with no TTY, and
# without it the installer stops at a confirmation prompt. `--no-channel-add`
# skips the nixpkgs channel for root, which no agent can use anyway (channels
# are per-user) and which a flake-based setup never reads.
#
# TRUSTED-USERS IS THE PRIVILEGED ACCOUNT ONLY, never an agent. A trusted user
# can point the daemon at any substituter and push arbitrary paths into the
# shared store, which another workspace's agent would then run - straight
# through the account split the driver exists to enforce. Written explicitly
# rather than left to the installer's default, because it is a security
# boundary and a default can move.
#
# GARBAGE COLLECTION: no timer. A periodic `nix-collect-garbage` is a policy the
# box cannot make well - the store is shared by every attached workspace, and a
# sweep would drop the closures that make each one's next rebuild fast, at a
# moment nobody chose. What the daemon does instead is `min-free`/`max-free`: it
# collects unreferenced paths mid-build, and only once the disk is actually
# running short. Every live profile and its older generations are GC roots, so
# that reclaims build byproducts and superseded downloads, never a workspace's
# current environment. A workspace's own closure goes when its profile goes (an
# explicit `nix-collect-garbage` then frees it), or when the box is recreated.
#
# Idempotency: the installer runs only when /nix or the daemon is missing;
# nix.conf is rewritten on every run and the daemon restarted only when the
# file's content changed.
#
# Context (from the environment): DEVBOX_ADMIN_USER.
set -euo pipefail

NIX_VERSION="2.35.2"

: "${DEVBOX_ADMIN_USER:?nix mod: DEVBOX_ADMIN_USER is not set}"

# Already root (runas: root), so no privilege escalation here - a mod body has
# none anyway.
daemon_present() {
  systemctl is-enabled --quiet nix-daemon.socket 2>/dev/null \
    || systemctl is-active --quiet nix-daemon.service 2>/dev/null
}

if [ -x /nix/var/nix/profiles/default/bin/nix ] && daemon_present; then
  echo "nix: /nix and nix-daemon are already present; skipping the installer"
elif [ -e /nix ]; then
  # A half-installed store: the installer refuses to write over an existing
  # /nix, and guessing which half is missing is worse than saying so.
  echo "nix: /nix exists but nix-daemon is not running. Remove the partial" >&2
  echo "  install by hand (see the nix manual's uninstall section), or" >&2
  echo "  recreate the box." >&2
  exit 1
else
  echo "==> installing nix $NIX_VERSION (multi-user, daemon)"
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  curl -fsSL -o "$work/install-nix" \
    "https://releases.nixos.org/nix/nix-${NIX_VERSION}/install"
  sh "$work/install-nix" --daemon --yes --no-channel-add
fi

# The build-users group the installer made (its own default is `nixbld`), read
# back rather than assumed: the daemon needs the group that actually exists.
build_group="$(sed -n 's/^[[:space:]]*build-users-group[[:space:]]*=[[:space:]]*//p' \
  /etc/nix/nix.conf 2>/dev/null | tail -n1)"
build_group="${build_group:-nixbld}"

# The whole of /etc/nix/nix.conf, so that what the box runs is exactly what is
# written here. min-free/max-free are byte counts (5 GiB / 20 GiB): the daemon
# starts collecting when free space falls below the first and stops at the
# second.
conf="$(mktemp)"
cat > "$conf" <<EOF
# Managed by devbox (mods/nix.sh); rewritten on every box provisioning run.
build-users-group = $build_group
experimental-features = nix-command flakes
trusted-users = root $DEVBOX_ADMIN_USER
min-free = 5368709120
max-free = 21474836480
EOF

if ! cmp -s "$conf" /etc/nix/nix.conf; then
  install -D -m 0644 "$conf" /etc/nix/nix.conf
  # try-restart leaves a stopped daemon stopped, which is the normal state on a
  # box where systemd starts it from its socket on first use.
  systemctl try-restart nix-daemon.service || true
fi
rm -f "$conf"

nix_bin=/nix/var/nix/profiles/default/bin/nix
[ -x "$nix_bin" ] || {
  echo "nix: $nix_bin is missing after the install" >&2
  exit 1
}
"$nix_bin" --version
