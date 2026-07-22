#!/usr/bin/env bash
#
# devbox provisioning driver: runs an ordered set of idempotent "mods". Ships
# with devbox and is delivered two ways over the SAME payload (see provision.py):
#
#   * first boot  - the cloud-init seed stages the payload under
#                   /usr/local/lib/devbox-provision and a runcmd runs this driver
#                   as the dev user.
#   * reconverge  - `devbox provision` tars the payload, pipes it over SSH into a
#                   mktemp dir, and runs this driver there.
#
# Layout next to this script:  mods/<name>.sh  manifest  context.env  [secret.env]
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

# devbox-owned shell plumbing: make ~/.local/bin (where jj etc. install) and any
# ~/.cargo/env available to non-interactive shells via BASH_ENV. Must exist
# before the mods run. Overwritten unconditionally (idempotent).
cat > "$HOME/.bash_env" <<'BENV'
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
BENV
export PATH="$HOME/.local/bin:$PATH"

# Context for the mods via the environment (not %TOKEN% substitution). The
# non-secret file is safe to trace; the secret file (the GitHub PAT) is sourced
# with no tracing enabled here, so it never reaches a log.
# shellcheck source=/dev/null
[ -f "$HERE/context.env" ] && . "$HERE/context.env"
# shellcheck source=/dev/null
[ -f "$HERE/secret.env" ] && . "$HERE/secret.env"

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
