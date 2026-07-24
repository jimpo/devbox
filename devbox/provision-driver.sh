#!/usr/bin/env bash
#
# devbox provisioning driver: runs an ordered set of idempotent "mods". Ships
# with devbox and delivered entirely over SSH (see provision.py): devbox tars the
# payload, pipes it to the box, extracts into a per-run temp dir (owned by the
# SSH user), and runs this driver there - on `devbox create` (after boot) and on
# `devbox sync --mods` alike.
#
# Layout next to this script:  mods/<name>.sh  manifest  context.env
# ([env] creds are delivered separately as ~/.config/devbox/env, sourced below.)
#
# Usage: provision-driver.sh [<mod-name> ...]
#   no args   - run the whole playbook (manifest order)
#   mod names - run only those (fast iteration while writing one mod); each must
#               be in the playbook.
#
# Runs each mod in order, stops at the first failure and reports which mod, and
# tees each mod's output to ~/provision/<name>.log (the combined ~/provision.log
# that `devbox status` tails is produced by the outer tee in the caller).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODS_DIR="$HERE/mods"
LOG_DIR="$HOME/provision"
mkdir -p "$LOG_DIR" "$HOME/.local/bin"

# devbox-owned shell plumbing: make ~/.local/bin (where jj etc. install), any
# ~/.cargo/env, and the devbox-managed [env] creds file available to
# non-interactive shells via BASH_ENV (what Claude Code's Bash tool reads). Must
# exist before the mods run. Overwritten unconditionally (idempotent).
cat > "$HOME/.bash_env" <<'BENV'
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
[ -f "$HOME/.config/devbox/env" ] && . "$HOME/.config/devbox/env"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
BENV
export PATH="$HOME/.local/bin:$PATH"

# Credentials/vars from [env] (written 0600 by `devbox sync --envvars`, which
# runs before the mods on create). Sourced here with no tracing enabled, so a
# mod that needs a cred at provision time - e.g. repo-bootstrap's private
# `jj git clone` needing GITHUB_TOKEN - sees it, and it never reaches a log.
# shellcheck source=/dev/null
[ -f "$HOME/.config/devbox/env" ] && . "$HOME/.config/devbox/env"
# Provisioning context for the mods (DEVBOX_*), safe to trace.
# shellcheck source=/dev/null
[ -f "$HERE/context.env" ] && . "$HERE/context.env"

mapfile -t PLAYBOOK < "$HERE/manifest"

if [ "$#" -gt 0 ]; then
  # Selective run: every requested name must be in this project's playbook.
  declare -A known=()
  for m in "${PLAYBOOK[@]}"; do known["$m"]=1; done
  for want in "$@"; do
    if [ -z "${known[$want]:-}" ]; then
      printf 'devbox: unknown mod %q (this project runs: %s)\n' "$want" "${PLAYBOOK[*]}" >&2
      exit 2
    fi
  done
  PLAYBOOK=("$@")
fi

for name in "${PLAYBOOK[@]}"; do
  file="$MODS_DIR/$name.sh"
  if [ ! -f "$file" ]; then
    echo "devbox: mod script missing: $file" >&2
    exit 127
  fi
  echo "==> [$name] provisioning ..."
  # pipefail (set above) makes the mod's failure - not tee's success - the
  # pipeline's status, so a failing mod stops the run.
  if ! bash "$file" 2>&1 | tee "$LOG_DIR/$name.log"; then
    echo "devbox: mod '$name' FAILED - stopping (see ~/provision/$name.log)" >&2
    exit 1
  fi
  echo "==> [$name] ok"
done

echo "provisioning complete"
