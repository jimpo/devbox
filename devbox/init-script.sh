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
# This covers only genuine first-boot concerns: the privileged user (+ its SSH
# key), a baseline of packages the base mods lean on, the BASH_ENV wiring, and
# the hostname. The actual provisioning is modular "mods" (jj, dotfiles, Claude
# Code, repo bootstrap, and a workspace's own), driven entirely over SSH after
# boot by `devbox create` / `devbox sync --mods` (see cli.py / provision.py) -
# nothing of it is injected here, and a workspace's own first-boot needs are
# served by a workspace mod, not by this file.
#
# A box has TWO accounts: the privileged one created here, and the unprivileged
# `agent_user` that AI agents and the repo checkout live under. The agent user
# is deliberately NOT created here - this script runs once, at first boot, so an
# account born here could never appear on an existing box and changing
# `agent_user` would need a rebuild. It is created by the provisioning driver
# instead, which reconverges.
#
# Runs as root. `set -e` means a failed step aborts the rest of first boot (and
# surfaces as a non-zero `cloud-init status --wait`), so every step that talks
# to the network is retried, and the login user + its SSH key are created FIRST
# - before the package install - so the box stays reachable (and debuggable)
# even if a later step fails.
# Lightsail runs instance user-data under /bin/sh (dash), ignoring the shebang;
# re-exec under bash so the bashisms below work. Harmless on EC2/libvirt (bash
# already). MUST stay the first executable line - everything after it, starting
# with `set -o pipefail`, is a bashism dash chokes on.
if [ -z "${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi
set -euo pipefail

# ---- rendered tokens --------------------------------------------------------
# Single-quoted so nothing in a substituted value is expanded by the shell.
# render_init_script rejects values containing a single quote or a newline, so
# these assignments cannot be broken out of.
DEVBOX_USER='%DEVBOX_USER%'
DEVBOX_HOME='/home/%DEVBOX_USER%'
DEVBOX_SSH_PUBKEY='%DEVBOX_SSH_PUBKEY%'
DEVBOX_HOSTNAME='%DEVBOX_HOSTNAME%'
# Makes provisioned tools (jj / claude / a workspace's own rustup, ...) available
# to NON-interactive bash (e.g. Claude Code's Bash tool): PAM exports BASH_ENV
# from /etc/environment into the user's shells.
#
# It points at a SHARED STUB, not at a user's own file: /etc/environment is one
# system-wide table and the box has two accounts, so a per-user path here would
# make one user source the other's file. `BASH_ENV=$HOME/.bash_env` is not an
# option either - pam_env reads /etc/environment as plain KEY=VALUE pairs, and
# expansion there is not something to rely on (HOME is not necessarily even set
# at pam_env time). The stub defers the expansion to bash, where it works. Each
# user's own ~/.bash_env is written by the provisioning driver, which runs
# later, over SSH.
BASH_ENV_STUB='/etc/devbox/bash_env'
BASH_ENV_LINE="BASH_ENV=$BASH_ENV_STUB"

DEVBOX_PACKAGES=(
  zsh              # the agent account's login shell (the driver sets it when
                   # creating one); the privileged account stays on bash -
                   # see step 5 below
  vim
  python3          # used by the `jimpo` oh-my-zsh theme's prompt_pwd
  curl
  git
  git-lfs          # gitconfig declares the lfs clean/smudge filters
  ca-certificates
  gh               # GitHub CLI; also git's HTTPS credential helper
  rsync            # receiver for `devbox sync` (host->box file push)
  # The agent account has no sudo, so it cannot apt-install its way out of a
  # missing build dependency mid-task the way the old single sudo-capable user
  # could - it takes a `packages` entry on a mod and a `devbox sync --mods`.
  # Carry the two that virtually every native build wants. Anything beyond this
  # belongs in a mod: first boot is slow enough, and it is slow for every box.
  build-essential
  pkg-config
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

# ---- 1. privileged user, SSH key, sudo (FIRST: never gated on packages) ------
log "creating user '$DEVBOX_USER'"
if ! id -u "$DEVBOX_USER" >/dev/null 2>&1; then
  # Created with /bin/bash - zsh isn't installed yet, and a login shell that
  # doesn't exist locks the account out. Switched to zsh after the install.
  # HOME_MODE 0750, not the distro default 0755: the unprivileged agent account
  # (created later by the driver) must not be able to read this user's home,
  # which is the side of the box that holds privilege.
  # A group of the account's own name may already exist - Ubuntu's cloud images
  # ship `admin`, its legacy sudo group, which is exactly the default account
  # name - and `--user-group` refuses to create one that does. Join it instead:
  # this account gets passwordless sudo below anyway, so membership grants it
  # nothing new.
  if getent group "$DEVBOX_USER" >/dev/null; then
    group_opt=(--gid "$DEVBOX_USER")
  else
    group_opt=(--user-group)
  fi
  useradd --create-home --home-dir "$DEVBOX_HOME" "${group_opt[@]}" \
          --key HOME_MODE=0750 --groups sudo --shell /bin/bash "$DEVBOX_USER"
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
# The stub every user's non-interactive bash runs, which then sources that
# user's own ~/.bash_env (written per-user by the provisioning driver). Ends in
# `:` so a user without the file yet still leaves bash a zero status.
log "installing the $BASH_ENV_STUB shim"
install -d -m 0755 "$(dirname "$BASH_ENV_STUB")"
cat > "$BASH_ENV_STUB" <<'STUB'
# devbox: per-user BASH_ENV shim. /etc/environment is system-wide and the box
# has more than one account, so it points here and the per-user file is
# resolved by bash, which can actually expand $HOME.
if [ -r "$HOME/.bash_env" ]; then . "$HOME/.bash_env"; fi
:
STUB
chmod 0644 "$BASH_ENV_STUB"

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
# The privileged account stays on the /bin/bash step 1 created it with,
# deliberately: it is the box's debugging entry point (`devbox box ssh`), and
# has no per-account setup (no dotfiles, no oh-my-zsh) to switch a shell for.
# `zsh` is still installed - it is the login shell the driver sets for every
# agent account it creates.

log "first-boot init complete"
