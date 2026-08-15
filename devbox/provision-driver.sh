#!/usr/bin/env bash
#
# devbox provisioning driver: runs an ordered set of idempotent "mods". Ships
# with devbox and delivered entirely over SSH (see provision.py): devbox tars the
# payload, pipes it to the box, extracts into a per-run temp dir, and runs this
# driver there - on `devbox create` (after boot) and on `devbox sync --mods`
# alike.
#
# Layout next to this script:  mods/<name>.sh  manifest  context.env
# ([env] creds are delivered separately as the agent's ~/.config/devbox/env.)
#
# THE DRIVER RUNS AS THE PRIVILEGED USER and drops privilege per mod. A box has
# two accounts: the privileged one (created at first boot by the init script,
# the SSH target this arrives on) and the unprivileged `agent` (created HERE, in
# the preamble below - not at first boot, so that changing it reconverges).
# Almost every mod configures a user's home directory, and the user it should
# configure is the agent, so `# runas:` defaults to the agent and a mod body
# runs WITHOUT sudo. System packages are declared `# packages:` and installed by
# this driver; the rare mod that truly needs privilege says `# runas: root`.
#
# Each mod body sources the `[env]` creds of the account that owns them: its own
# target user's ~/.config/devbox/env, except a `root` mod, which reads the
# privileged user's (root has no file of its own, and root is that account's
# privileged alter ego). Which creds an account gets is the `owner` of each
# `[env]` entry, host-side.
#
# Usage: provision-driver.sh [--ensure-users | <mod-name> ...]
#   no args        - run the whole playbook (manifest order)
#   --ensure-users - run only the user preamble and exit (the CLI calls this
#                    before writing the agent's [env] file, which needs the home
#                    directory to exist)
#   mod names      - run only those (fast iteration while writing one mod); each
#                    must be in the playbook.
#
# Runs each mod in order, stops at the first failure and reports which mod, and
# tees each mod's output to ~/provision/<name>.log (the combined ~/provision.log
# that `devbox status` tails is produced by the outer tee in the caller).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODS_DIR="$HERE/mods"
LOG_DIR="$HOME/provision"
mkdir -p "$LOG_DIR"

fail() { echo "devbox: $*" >&2; exit 1; }

# Provisioning context for the mods (DEVBOX_*), safe to trace. Carries both
# usernames and the host's public key, which the preamble authorizes for the
# agent account. No secrets - that is what lets the payload be agent-readable.
# shellcheck source=/dev/null
[ -f "$HERE/context.env" ] && . "$HERE/context.env"
: "${DEVBOX_ADMIN_USER:?context.env did not define DEVBOX_ADMIN_USER}"
: "${DEVBOX_AGENT_USER:?context.env did not define DEVBOX_AGENT_USER}"

# The mod bodies run as another user, so they must be able to read the payload.
# `mktemp -d` gives 0700; the payload carries no secrets (see provision.py).
chmod 0755 "$HERE" 2>/dev/null || true

# ---- user preamble (privileged, idempotent) ---------------------------------
# Groups that are root-equivalent in practice: a member can trivially become
# root, so the agent being in one would make the whole split decorative.
HARD_PRIVILEGED_GROUPS=(sudo admin root wheel)
SOFT_PRIVILEGED_GROUPS=(docker lxd)

user_home() { getent passwd "$1" | cut -d: -f6; }

# The per-user shell plumbing devbox owns: puts ~/.local/bin (where jj, claude
# etc. install), any ~/.cargo/env, and that user's [env] creds on the path of
# NON-interactive bash - what Claude Code's Bash tool reads. Reached via
# BASH_ENV=/etc/devbox/bash_env (wired system-wide at first boot), which sources
# whichever user's file applies. Overwritten unconditionally (idempotent).
install_bash_env() {
  local user="$1" home group
  home="$(user_home "$user")"
  group="$(id -gn "$user")"
  [ -n "$home" ] || fail "could not resolve the home directory of '$user'"
  sudo install -d -o "$user" -g "$group" -m 0755 "$home/.local/bin"
  sudo install -o "$user" -g "$group" -m 0644 /dev/stdin "$home/.bash_env" <<'BENV'
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
[ -f "$HOME/.config/devbox/env" ] && . "$HOME/.config/devbox/env"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
BENV
}

ensure_users() {
  local agent="$DEVBOX_AGENT_USER" admin="$DEVBOX_ADMIN_USER" shell=/bin/bash home groups g

  [ "$agent" != "$admin" ] || fail "agent and privileged user are both '$agent'"

  # zsh is installed by the init script, so it exists by the time the driver
  # first runs - but never point a login shell at a binary that isn't there.
  [ -x /usr/bin/zsh ] && shell=/usr/bin/zsh

  if ! id -u "$agent" >/dev/null 2>&1; then
    echo "==> creating the unprivileged agent user '$agent'"
    # No --groups: the agent is in no supplementary group at all, and above all
    # not sudo. HOME_MODE 0750 so the two accounts cannot read each other.
    sudo useradd --create-home --user-group --key HOME_MODE=0750 \
                 --shell "$shell" "$agent" \
      || fail "useradd '$agent' failed"
  fi
  # No password login, ever (key-only), matching the init script's own user.
  sudo passwd --lock "$agent" >/dev/null || fail "could not lock the password for '$agent'"

  # Assert the isolation actually holds rather than assuming it. A partial sudo
  # grant is not a lesser version of this - `apt-get install ./x.deb` runs
  # maintainer scripts as root - so any hard-privileged group is fatal.
  groups="$(id -nG "$agent" 2>/dev/null || true)"
  for g in "${HARD_PRIVILEGED_GROUPS[@]}"; do
    case " $groups " in
      *" $g "*) fail "agent user '$agent' is in the '$g' group, which is root-equivalent;
  the point of the split is that agents run without privilege. Remove it with
  'sudo deluser $agent $g', or pick a different agent_user." ;;
    esac
  done
  if sudo -l -U "$agent" >/dev/null 2>&1; then
    fail "agent user '$agent' has sudo privileges (check /etc/sudoers.d); the point
  of the split is that agents run without privilege."
  fi
  for g in "${SOFT_PRIVILEGED_GROUPS[@]}"; do
    case " $groups " in
      *" $g "*) echo "devbox: WARNING: agent user '$agent' is in the '$g' group, which is" >&2
                echo "  root-equivalent in practice - the isolation is decorative while it is." >&2 ;;
    esac
  done

  # The same host key that reaches the privileged user at first boot. The agent
  # alias needs a direct login: `jj git fetch` runs git-upload-pack as the login
  # user, and there is no sudo-ing through that.
  if [ -n "${DEVBOX_SSH_PUBKEY:-}" ]; then
    home="$(user_home "$agent")"
    [ -n "$home" ] || fail "could not resolve the home directory of '$agent'"
    sudo install -d -o "$agent" -g "$agent" -m 0700 "$home/.ssh"
    sudo touch "$home/.ssh/authorized_keys"
    sudo chown "$agent:$agent" "$home/.ssh/authorized_keys"
    sudo chmod 0600 "$home/.ssh/authorized_keys"
    # Idempotent: re-running must not duplicate the key.
    if ! sudo grep -qxF "$DEVBOX_SSH_PUBKEY" "$home/.ssh/authorized_keys"; then
      printf '%s\n' "$DEVBOX_SSH_PUBKEY" | sudo tee -a "$home/.ssh/authorized_keys" >/dev/null
    fi
  fi

  # Homes 0750 both ways. useradd only applies HOME_MODE to a home it creates,
  # so a pre-existing account (or a box created before this) still needs it.
  for u in "$agent" "$admin"; do
    home="$(user_home "$u")"
    [ -n "$home" ] && sudo chmod 0750 "$home"
  done

  install_bash_env "$agent"
  install_bash_env "$admin"

  # A repo checkout in the privileged user's home means this box was provisioned
  # under the old single-user model. Nothing is migrated - the canonical checkout
  # is now the agent's - so say so loudly rather than leaving two diverging trees.
  if [ "${DEVBOX_REPO_MODE:-none}" != none ] && [ -n "${DEVBOX_REPO_DIR:-}" ] \
     && [ -d "$(user_home "$admin")/$DEVBOX_REPO_DIR" ]; then
    echo "devbox: WARNING: '$DEVBOX_REPO_DIR' exists in ${admin}'s home. This box predates" >&2
    echo "  the two-user split; the checkout devbox now uses is ${agent}'s. The old tree is" >&2
    echo "  left untouched and is NOT synced - recreate the box, or salvage it by hand." >&2
  fi
}

# ---- apt (mods cannot install their own packages) ---------------------------
APT_UPDATED=0

apt_ensure_updated() {
  [ "$APT_UPDATED" -eq 0 ] || return 0
  sudo apt-get -o DPkg::Lock::Timeout=600 update -qq \
    || fail "apt-get update failed"
  APT_UPDATED=1
}

install_packages() {
  local list="$1" pkgs
  [ -n "$list" ] || return 0
  IFS=',' read -r -a pkgs <<< "$list"
  apt_ensure_updated
  echo "==> installing packages: ${pkgs[*]}"
  sudo env DEBIAN_FRONTEND=noninteractive \
    apt-get -o DPkg::Lock::Timeout=600 install -y "${pkgs[@]}"
}

# ---- mod execution ----------------------------------------------------------
target_user() {
  case "$1" in
    agent) printf '%s' "$DEVBOX_AGENT_USER" ;;
    admin) printf '%s' "$DEVBOX_ADMIN_USER" ;;
    root)  printf '%s' root ;;
    *)     return 1 ;;
  esac
}

# The account whose [env] creds a `# runas:` target reads. root has no env file
# of its own; it gets the privileged user's, so a mod that needs both privilege
# and a credential (`owner = "admin"` host-side) can have both.
env_owner() {
  case "$1" in
    agent)       printf '%s' "$DEVBOX_AGENT_USER" ;;
    admin | root) printf '%s' "$DEVBOX_ADMIN_USER" ;;
  esac
}

# Run one mod body as one target user, with the creds of `env_home`'s account.
# sudo resets the environment, so the context and those creds are re-established
# INSIDE the target shell by sourcing files - never passed as `sudo env VAR=...`,
# which would put secrets in the process argv where any user on the box could
# read them out of `ps`.
run_mod_as() {
  local file="$1" user="$2" env_home="$3"
  sudo -u "$user" -H bash -c '
    if [ -f "$3/.config/devbox/env" ]; then . "$3/.config/devbox/env"; fi
    . "$1/context.env"
    exec bash "$2"
  ' _ "$HERE" "$file" "$env_home"
}

# ---- manifest ---------------------------------------------------------------
# `<name>\t<runas,...>\t<packages,...>` per line, in playbook order.
[ -f "$HERE/manifest" ] || fail "payload manifest missing at $HERE/manifest"
MOD_NAMES=()
MOD_RUNAS=()
MOD_PACKAGES=()
while IFS=$'\t' read -r m_name m_runas m_packages; do
  [ -n "$m_name" ] || continue
  MOD_NAMES+=("$m_name")
  MOD_RUNAS+=("${m_runas:-agent}")
  MOD_PACKAGES+=("${m_packages:-}")
done < "$HERE/manifest"

SELECTED=()
if [ "$#" -gt 0 ] && [ "$1" = "--ensure-users" ]; then
  ensure_users
  exit 0
elif [ "$#" -gt 0 ]; then
  # Selective run: every requested name must be in this project's playbook.
  declare -A known=()
  for i in "${!MOD_NAMES[@]}"; do known["${MOD_NAMES[$i]}"]="$i"; done
  for want in "$@"; do
    # `+set` tests key presence, not value: index 0 is a legitimate value that
    # `-z "${known[$want]:-}"` would misread as "not in the playbook".
    if [ -z "${known[$want]+set}" ]; then
      printf 'devbox: unknown mod %q (this project runs: %s)\n' \
        "$want" "${MOD_NAMES[*]}" >&2
      exit 2
    fi
    SELECTED+=("${known[$want]}")
  done
else
  for i in "${!MOD_NAMES[@]}"; do SELECTED+=("$i"); done
fi

# Always, before any mod: the agent account a mod body is about to drop into.
ensure_users

for i in "${SELECTED[@]}"; do
  name="${MOD_NAMES[$i]}"
  file="$MODS_DIR/$name.sh"
  if [ ! -f "$file" ]; then
    echo "devbox: mod script missing: $file" >&2
    exit 127
  fi

  install_packages "${MOD_PACKAGES[$i]}" || {
    echo "devbox: mod '$name' FAILED installing its '# packages:' - stopping" >&2
    exit 1
  }

  IFS=',' read -r -a targets <<< "${MOD_RUNAS[$i]}"
  for target in "${targets[@]}"; do
    if ! user="$(target_user "$target")"; then
      echo "devbox: mod '$name' has an unknown '# runas:' target '$target'" >&2
      exit 2
    fi
    # One log per (mod, user) only when a mod actually runs more than once, so
    # the common case keeps the ~/provision/<name>.log path people know.
    if [ "${#targets[@]}" -gt 1 ]; then
      log_file="$LOG_DIR/$name.$user.log"
      echo "==> [$name] provisioning as $user ..."
    else
      log_file="$LOG_DIR/$name.log"
      echo "==> [$name] provisioning (as $user) ..."
    fi
    # pipefail (set above) makes the mod's failure - not tee's success - the
    # pipeline's status, so a failing mod stops the run.
    if ! run_mod_as "$file" "$user" "$(user_home "$(env_owner "$target")")" \
         2>&1 | tee "$log_file"; then
      echo "devbox: mod '$name' FAILED as '$user' - stopping (see ${log_file/#$HOME/\~})" >&2
      exit 1
    fi
  done
  echo "==> [$name] ok"
done

echo "provisioning complete"
