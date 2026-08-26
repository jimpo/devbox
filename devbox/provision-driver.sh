#!/usr/bin/env bash
#
# devbox provisioning driver: runs an ordered set of idempotent "mods". Ships
# with devbox and delivered entirely over SSH (see provision.py): devbox tars the
# payload, pipes it to the box, extracts into a per-run temp dir, and runs this
# driver there - on `devbox create` (after boot) and on `devbox sync --mods`
# alike.
#
# Layout next to this script:  mods/<name>.sh  manifest  context.env  repos
# ([env] creds are delivered separately as the agent's ~/.config/devbox/env.)
#
# THE DRIVER RUNS AS THE PRIVILEGED USER and drops privilege per mod. A box has
# one privileged account (created at first boot by the init script, the SSH
# target this arrives on) and one unprivileged `agent` per attached workspace,
# created HERE, in the preamble below - not at first boot, so that attaching a
# workspace reconverges an existing box instead of needing a new one. Almost every
# mod configures a user's home directory, and the user it should configure is the
# agent, so a mod body runs WITHOUT sudo. System packages are installed by this
# driver instead, and the rare mod that truly needs privilege gets a different
# runas target.
#
# THIS SCRIPT NEVER PARSES A MOD. Mod metadata (the `# /// mod` TOML block) is
# read host-side by provision.py, which resolves, validates and orders the
# playbook and hands over the result as `manifest`. What arrives here is already
# a decision - name, runas targets, packages - so the columns below are the only
# vocabulary this script has, and a mod file is opaque to it: it is executed,
# never read.
#
# TWO KINDS OF RUN, distinguished by whether DEVBOX_AGENT_USER is set:
#   attachment run - one workspace's playbook, in its account (the usual case)
#   box run        - the box's own playbook (empty DEVBOX_AGENT_USER); no agent
#                    account is created or touched
# Which mod runs in which is decided host-side: the manifest that arrives has
# already been narrowed to one scope, so this script never reasons about scopes.
#
# Each mod body sources the `[env]` creds of the account that owns them: its own
# target user's ~/.config/devbox/env, except a `root` mod, which reads the
# privileged user's (root has no file of its own, and root is that account's
# privileged alter ego). Which creds an account gets is the `owner` of each
# `[env]` entry, host-side.
#
# Usage: provision-driver.sh [--ensure-users|--ensure-admin|--ensure-agent | <mod-name> ...]
#   no args        - run the whole playbook (manifest order)
#   --ensure-*     - run only that half of the user preamble and exit (the CLI
#                    calls --ensure-agent before writing the agent's [env] file,
#                    which needs the home directory to exist); --ensure-users
#                    means both.
#   mod names      - run only those (fast iteration while writing one mod); each
#                    must be in the playbook.
#
# Runs each mod in order, stops at the first failure and reports which mod, and
# tees each mod's output to ~/provision/<agent>/<name>.log - one directory per
# attachment, so two workspaces reconverging don't overwrite each other's logs.
# (The combined ~/provision-<agent>.log that `devbox status` tails is produced by
# the outer tee in the caller.)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODS_DIR="$HERE/mods"

fail() { echo "devbox: $*" >&2; exit 1; }

# Provisioning context for the mods (DEVBOX_*), safe to trace. Carries the
# usernames and the host's public key, which the preamble authorizes for the
# agent account. No secrets - that is what lets the payload be agent-readable.
# shellcheck source=/dev/null
[ -f "$HERE/context.env" ] && . "$HERE/context.env"
: "${DEVBOX_ADMIN_USER:?context.env did not define DEVBOX_ADMIN_USER}"
# Empty on a box run - the one signal that this run has no attachment.
DEVBOX_AGENT_USER="${DEVBOX_AGENT_USER:-}"

LOG_DIR="$HOME/provision/${DEVBOX_AGENT_USER:-box}"
mkdir -p "$LOG_DIR"

# Who owns which unprivileged account, written by the preamble and read by
# `devbox box list --attached` / `devbox box destroy`. Root-owned and in /etc
# rather than in the account's home, because homes are 0750 and this has to stay
# enumerable by the privileged user. Root-owned but world-READABLE: only writing
# a claim needs privilege, and checking one has to work without it.
# (Overridable so the test harness can run the real script without root.)
WORKSPACES_DIR="${DEVBOX_WORKSPACES_DIR:-/etc/devbox/workspaces}"
# Where the same claims were written when an attachment was called a project.
# Read-only, and only when the account has no current marker: re-attaching writes
# the current one, so a box migrates itself the first time devbox touches it.
LEGACY_MARKERS_DIR="${DEVBOX_LEGACY_MARKERS_DIR:-/etc/devbox/projects}"

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
  # Both paths, not just the leaf: install(1) gives a directory it creates as a
  # parent the default root ownership, and an agent that cannot write ~/.local
  # cannot install anything into it.
  sudo install -d -o "$user" -g "$group" -m 0755 "$home/.local" "$home/.local/bin"
  # install(1) on Ubuntu (uutils coreutils) fails to overwrite an existing file
  # when the source is /dev/stdin, so clear the destination first. install still
  # creates it with the final mode, so it is never briefly world-readable.
  sudo rm -f "$home/.bash_env"
  sudo install -o "$user" -g "$group" -m 0644 /dev/stdin "$home/.bash_env" <<'BENV'
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
[ -f "$HOME/.config/devbox/env" ] && . "$HOME/.config/devbox/env"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
BENV
}

# The box's own account work: idempotent, and the only preamble a box run does.
ensure_admin() {
  local admin="$DEVBOX_ADMIN_USER" home
  home="$(user_home "$admin")"
  [ -n "$home" ] && sudo chmod 0750 "$home"
  install_bash_env "$admin"
}

# Refuse to take over an account that is not ours. An agent account is a
# workspace's home on a shared machine, so "someone already lives here" has to be
# an error rather than a silent co-tenancy: two workspaces sharing one home would
# share their checkouts and a credentials file.
marker_field() { sed -n "s/^$2=//p" "$1" | head -n1; }

# The marker recording this account's owner: the current one, or the one a
# devbox that still called workspaces "projects" left behind.
account_marker() {
  local agent="$1"
  if [ -f "$WORKSPACES_DIR/$agent" ]; then
    printf '%s' "$WORKSPACES_DIR/$agent"
  elif [ -f "$LEGACY_MARKERS_DIR/$agent" ]; then
    printf '%s' "$LEGACY_MARKERS_DIR/$agent"
  fi
}

check_account_claim() {
  local agent="$1" marker owner prev_host
  marker="$(account_marker "$agent")"

  if [ -z "$marker" ]; then
    [ "${DEVBOX_ATTACH_ADOPT:-0}" = "1" ] && return 0
    fail "the account '$agent' already exists on this box but devbox has no record
  of which workspace it belongs to (a box created before attachments were tracked,
  or an account somebody made by hand). Re-run with --adopt to claim it for
  '${DEVBOX_WORKSPACE:-this workspace}', or pick a different agent_user."
  fi

  # `project` is the same field under its old name, in a legacy marker.
  owner="$(marker_field "$marker" workspace)"
  [ -n "$owner" ] || owner="$(marker_field "$marker" project)"
  if [ -n "$owner" ] && [ -n "${DEVBOX_WORKSPACE:-}" ] && [ "$owner" != "$DEVBOX_WORKSPACE" ]; then
    fail "the account '$agent' on this box belongs to workspace '$owner'
  (attached $(marker_field "$marker" attached_at) from $(marker_field "$marker" host)).
  Set a different \`agent_user\` in this workspace's config, or detach '$owner' first."
  fi

  # Same workspace from a different workstation is fine - say so, since it means
  # two people (or two checkouts) are driving one account.
  prev_host="$(marker_field "$marker" host)"
  if [ -n "$prev_host" ] && [ -n "${DEVBOX_ATTACH_HOST:-}" ] \
     && [ "$prev_host" != "$DEVBOX_ATTACH_HOST" ]; then
    echo "devbox: note: '$agent' was last attached from '$prev_host'." >&2
  fi
}

# Record which workspace owns this account. Written via a temp file in the same
# directory + mv, so a reader never sees a half-written marker.
write_marker() {
  local agent="$1" marker="$WORKSPACES_DIR/$agent"
  sudo install -d -m 0755 "$WORKSPACES_DIR"
  # A leftover .tmp from a crashed run would defeat the install (see .bash_env
  # above) and then be promoted by the mv below as if it were this run's marker.
  sudo rm -f "$marker.tmp"
  sudo install -m 0644 /dev/stdin "$marker.tmp" <<EOF
workspace=${DEVBOX_WORKSPACE:-}
agent_user=$agent
repo_dirs=${DEVBOX_REPO_DIRS:-}
attached_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
host=${DEVBOX_ATTACH_HOST:-}
EOF
  sudo mv "$marker.tmp" "$marker"
  # The claim now lives under its current name; leaving the old one would keep
  # `--attached` reporting this account twice.
  [ "$WORKSPACES_DIR" != "$LEGACY_MARKERS_DIR" ] \
    && sudo rm -f "$LEGACY_MARKERS_DIR/$agent"
  return 0
}

ensure_agent() {
  local agent="$DEVBOX_AGENT_USER" admin="$DEVBOX_ADMIN_USER" shell=/bin/bash home groups g

  # A box run has no agent account at all - nothing to do here.
  [ -n "$agent" ] || return 0

  [ "$agent" != "$admin" ] || fail "the agent account and the box's privileged
  user are both '$agent'; the whole point of the split is that agents run
  without sudo. Set \`agent_user\` in this workspace's config."

  # zsh is installed by the init script, so it exists by the time the driver
  # first runs - but never point a login shell at a binary that isn't there.
  [ -x /usr/bin/zsh ] && shell=/usr/bin/zsh

  if id -u "$agent" >/dev/null 2>&1; then
    check_account_claim "$agent"
  else
    echo "==> creating the unprivileged agent user '$agent'"
    # No --groups: the agent is in no supplementary group at all, and above all
    # not sudo. HOME_MODE 0750 so accounts cannot read each other.
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
  # The listing is the answer, not the exit status: sudo-rs (Ubuntu's default
  # sudo) exits 0 whether or not the user is allowed. LC_ALL=C so the phrase
  # classic sudo prints is not translated out from under the match.
  if LC_ALL=C sudo -n -l -U "$agent" 2>/dev/null | grep -q 'may run the following commands'; then
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

  # Home 0750. useradd only applies HOME_MODE to a home it creates, so a
  # pre-existing account (or a box created before this) still needs it - and it
  # is what keeps one workspace's agent out of another's home.
  home="$(user_home "$agent")"
  [ -n "$home" ] && sudo chmod 0750 "$home"

  install_bash_env "$agent"
  write_marker "$agent"

  # A checkout in the privileged user's home means this box was provisioned under
  # the old single-user model. Nothing is migrated - the canonical checkouts are
  # now the agent's - so say so loudly rather than leaving two diverging trees.
  local admin_home repo_dir
  local -a repo_dirs
  admin_home="$(user_home "$admin")"
  IFS=',' read -r -a repo_dirs <<< "${DEVBOX_REPO_DIRS:-}"
  for repo_dir in "${repo_dirs[@]}"; do
    [ -n "$repo_dir" ] && [ -d "$admin_home/$repo_dir" ] || continue
    echo "devbox: WARNING: '$repo_dir' exists in ${admin}'s home. This box predates" >&2
    echo "  the two-user split; the checkout devbox now uses is ${agent}'s. The old tree is" >&2
    echo "  left untouched and is NOT synced - recreate the box, or salvage it by hand." >&2
  done
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
    # An `agent` target in a box run has no account to run as. The host narrows
    # the manifest by scope so this should be unreachable; treat it as a bug in
    # the payload rather than silently running the mod as somebody else.
    agent) [ -n "$DEVBOX_AGENT_USER" ] || return 1
           printf '%s' "$DEVBOX_AGENT_USER" ;;
    admin) printf '%s' "$DEVBOX_ADMIN_USER" ;;
    root)  printf '%s' root ;;
    *)     return 1 ;;
  esac
}

# The account whose [env] creds a runas target reads. root has no env file
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
#
# The body runs from the target account's own home: sudo carries the caller's
# working directory across, and this driver's is the privileged user's home,
# which an agent account has no permission to enter. A mod that only uses
# absolute paths does not care, but one whose tools forward their cwd does.
run_mod_as() {
  local file="$1" user="$2" env_home="$3"
  sudo -u "$user" -H bash -c '
    cd "$HOME" || exit 1
    if [ -f "$3/.config/devbox/env" ]; then . "$3/.config/devbox/env"; fi
    . "$1/context.env"
    # Where the payload was unpacked, so a mod can read a file that is too long
    # to be an environment variable - `repos`, which repo-bootstrap walks.
    export DEVBOX_PAYLOAD_DIR="$1"
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
if [ "$#" -gt 0 ] && [ "${1#--ensure-}" != "$1" ]; then
  case "$1" in
    --ensure-admin) ensure_admin ;;
    --ensure-agent) ensure_agent ;;
    --ensure-users) ensure_admin; ensure_agent ;;
    *) fail "unknown option '$1'" ;;
  esac
  exit 0
elif [ "$#" -gt 0 ]; then
  # Selective run: every requested name must be in this workspace's playbook.
  declare -A known=()
  for i in "${!MOD_NAMES[@]}"; do known["${MOD_NAMES[$i]}"]="$i"; done
  for want in "$@"; do
    # `+set` tests key presence, not value: index 0 is a legitimate value that
    # `-z "${known[$want]:-}"` would misread as "not in the playbook".
    if [ -z "${known[$want]+set}" ]; then
      printf 'devbox: unknown mod %q (this workspace runs: %s)\n' \
        "$want" "${MOD_NAMES[*]}" >&2
      exit 2
    fi
    SELECTED+=("${known[$want]}")
  done
else
  for i in "${!MOD_NAMES[@]}"; do SELECTED+=("$i"); done
fi

# Always, before any mod: the accounts a mod body is about to drop into.
# ensure_agent is a no-op on a box run.
ensure_admin
ensure_agent

for i in "${SELECTED[@]}"; do
  name="${MOD_NAMES[$i]}"
  file="$MODS_DIR/$name.sh"
  if [ ! -f "$file" ]; then
    echo "devbox: mod script missing: $file" >&2
    exit 127
  fi

  install_packages "${MOD_PACKAGES[$i]}" || {
    echo "devbox: mod '$name' FAILED installing its packages - stopping" >&2
    exit 1
  }

  IFS=',' read -r -a targets <<< "${MOD_RUNAS[$i]}"
  for target in "${targets[@]}"; do
    if ! user="$(target_user "$target")"; then
      echo "devbox: mod '$name' has an unusable runas target '$target'" \
           "(this run has no agent account)" >&2
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
