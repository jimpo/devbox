#!/bin/bash
#
# devbox first-boot init script: the user-data handed to EVERY provider.
#
# Ships with devbox itself (initscript.py resolves it via Path(__file__).parent,
# so it works out of the box under `uv tool install --editable .`) and is
# rendered by `render_init_script` - literal %TOKEN% substitution, no templating
# engine - before being handed to the provider as user-data:
#
#   libvirt  NoCloud seed  -> cloud-init runs it (scripts-user stage)
#   ec2      UserData      -> cloud-init runs it (scripts-user stage)
#   aws      Lightsail userData -> Lightsail runs it natively as a shell script
#
# A shell script is the ONLY user-data format all three accept: Lightsail
# executes user-data as a script and does not understand `#cloud-config`, which
# is why devbox has no cloud-config layer at all.
#
# This covers only genuine first-boot concerns: the login user (+ its SSH key),
# a baseline of packages the base mods lean on, the BASH_ENV wiring, and the
# hostname. The actual provisioning is modular "mods" (jj, dotfiles, Claude
# Code, repo bootstrap, and a project's own), driven entirely over SSH after
# boot by `devbox create` / `devbox sync --mods` (see cli.py / provision.py) -
# nothing of it is injected here, and a project's own first-boot needs are
# served by a project mod, not by this file.
#
# Runs as root. `set -e` means a failed step aborts the rest of first boot (and
# surfaces as a non-zero `cloud-init status --wait`), so every step that talks
# to the network is retried, and the login user + its SSH key are created FIRST
# - before the package install - so the box stays reachable (and debuggable)
# even if a later step fails.
set -euo pipefail

# ---- rendered tokens --------------------------------------------------------
# Single-quoted so nothing in a substituted value is expanded by the shell.
# render_init_script rejects values containing a single quote or a newline, so
# these assignments cannot be broken out of.
DEVBOX_USER='%DEVBOX_USER%'
DEVBOX_HOME='/home/%DEVBOX_USER%'
DEVBOX_SSH_PUBKEY='%DEVBOX_SSH_PUBKEY%'
DEVBOX_HOSTNAME='%DEVBOX_HOSTNAME%'
# Makes provisioned tools (jj / claude / a project's own rustup, ...) available
# to NON-interactive bash (e.g. Claude Code's Bash tool): PAM exports BASH_ENV
# from /etc/environment into the user's shells. The referenced file is written
# by the provisioning driver, which runs later, over SSH.
BASH_ENV_LINE='BASH_ENV=/home/%DEVBOX_USER%/.bash_env'

DEVBOX_PACKAGES=(
  zsh              # the login shell $DEVBOX_USER is switched to below
  vim
  python3          # used by the `jimpo` oh-my-zsh theme's prompt_pwd
  curl
  git
  git-lfs          # gitconfig declares the lfs clean/smudge filters
  ca-certificates
  gh               # GitHub CLI; also git's HTTPS credential helper
  rsync            # receiver for `devbox sync` (host->box file push)
)

export DEBIAN_FRONTEND=noninteractive

log() { printf 'devbox-init: %s\n' "$*"; }

# Retry a network-facing command with a linear backoff. A first-boot box races
# apt-daily/unattended-upgrades and cold mirrors; without this, one transient
# failure would abort the whole init under `set -e`.
retry() {
  local attempt=1 max=5
  until "$@"; do
    if [ "$attempt" -ge "$max" ]; then
      log "'$*' failed after $max attempts"
      return 1
    fi
    log "'$*' failed (attempt $attempt/$max); retrying in $((attempt * 5))s"
    sleep "$((attempt * 5))"
    attempt=$((attempt + 1))
  done
}

# ---- 1. login user, SSH key, sudo (FIRST: never gated on the package step) ---
log "creating user '$DEVBOX_USER'"
if ! id -u "$DEVBOX_USER" >/dev/null 2>&1; then
  # Created with /bin/bash - zsh isn't installed yet, and a login shell that
  # doesn't exist locks the account out. Switched to zsh after the install.
  useradd --create-home --home-dir "$DEVBOX_HOME" --user-group \
          --groups sudo --shell /bin/bash "$DEVBOX_USER"
fi
# No password login, ever (key-only), matching the cloud image's own users.
passwd --lock "$DEVBOX_USER" >/dev/null
# Its own group when we created the account, but read it back: the user may
# already exist (a base image whose default user is this name) with another one.
DEVBOX_GROUP="$(id -gn "$DEVBOX_USER")"

log "installing $DEVBOX_HOME/.ssh/authorized_keys"
install -d -m 0700 -o "$DEVBOX_USER" -g "$DEVBOX_GROUP" "$DEVBOX_HOME/.ssh"
touch "$DEVBOX_HOME/.ssh/authorized_keys"
chmod 0600 "$DEVBOX_HOME/.ssh/authorized_keys"
chown "$DEVBOX_USER:$DEVBOX_GROUP" "$DEVBOX_HOME/.ssh/authorized_keys"
# Idempotent: re-running must not duplicate the key.
if ! grep -qxF "$DEVBOX_SSH_PUBKEY" "$DEVBOX_HOME/.ssh/authorized_keys"; then
  printf '%s\n' "$DEVBOX_SSH_PUBKEY" >> "$DEVBOX_HOME/.ssh/authorized_keys"
fi

log "granting passwordless sudo"
# Fixed filename: sudo ignores drop-ins whose name contains a '.' or ends in
# '~', which a username-derived name could. Validated before it goes live - an
# unparsable drop-in breaks sudo for everyone on the box.
sudoers_tmp="$(mktemp)"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$DEVBOX_USER" > "$sudoers_tmp"
visudo -cf "$sudoers_tmp" >/dev/null
install -m 0440 -o root -g root "$sudoers_tmp" /etc/sudoers.d/90-devbox
rm -f "$sudoers_tmp"

# ---- 2. hostname ------------------------------------------------------------
log "setting hostname to '$DEVBOX_HOSTNAME'"
printf '%s\n' "$DEVBOX_HOSTNAME" > /etc/hostname
hostnamectl set-hostname "$DEVBOX_HOSTNAME" 2>/dev/null || hostname "$DEVBOX_HOSTNAME"
# Keep sudo/hostname resolution quiet ("unable to resolve host ...").
if ! grep -qE "^127\.0\.1\.1[[:space:]]+$DEVBOX_HOSTNAME\$" /etc/hosts; then
  printf '127.0.1.1 %s\n' "$DEVBOX_HOSTNAME" >> /etc/hosts
fi
# This script runs once (per-instance), but cloud-init re-applies the
# datasource's hostname on every boot; without this the name would revert on
# the first reboot.
if [ -d /etc/cloud/cloud.cfg.d ]; then
  printf 'preserve_hostname: true\n' > /etc/cloud/cloud.cfg.d/99-devbox-hostname.cfg
fi

# ---- 3. BASH_ENV ------------------------------------------------------------
log "wiring $BASH_ENV_LINE into /etc/environment"
touch /etc/environment
if ! grep -qxF "$BASH_ENV_LINE" /etc/environment; then
  printf '%s\n' "$BASH_ENV_LINE" >> /etc/environment
fi

# ---- 4. base packages -------------------------------------------------------
# `gh` is not in every Ubuntu release's archive (notably 24.04, the newest LTS
# blueprint Lightsail offers), and a missing package would abort first boot
# under `set -e`. The official GitHub CLI apt repo resolves it on every release
# devbox boots. curl + ca-certificates are preinstalled on Ubuntu cloud images.
log "adding the GitHub CLI apt repository"
install -m 0755 -d /etc/apt/keyrings
retry curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' \
  "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/github-cli.list

# Only the baseline the mods lean on - never a full distro upgrade (first boot
# is slow enough already; upgrading is the box owner's business).
log "installing base packages: ${DEVBOX_PACKAGES[*]}"
retry apt-get -o DPkg::Lock::Timeout=600 update
retry apt-get -o DPkg::Lock::Timeout=600 install -y "${DEVBOX_PACKAGES[@]}"

# ---- 5. login shell ---------------------------------------------------------
# Now that zsh exists (step 1 deliberately created the account with bash).
if [ -x /usr/bin/zsh ]; then
  log "setting $DEVBOX_USER's login shell to /usr/bin/zsh"
  usermod --shell /usr/bin/zsh "$DEVBOX_USER"
fi

log "first-boot init complete"
