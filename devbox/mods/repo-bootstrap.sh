#!/usr/bin/env bash
#
# mod: repo-bootstrap - get the project's code onto the box per [repo] in
# .devbox/config.toml. "clone" pulls an existing repo via jj; "push" leaves an
# empty colocated jj+git repo ready to receive `jj git push` from the host;
# "none" is a no-op. Ordered last in the base playbook (after jj + github-auth,
# so a private clone can authenticate).
#
# requires: jj github-auth
#
# Idempotency: guarded on the target directory, so a reconverge is a no-op once
# the repo exists.
#
# Context (from the environment): DEVBOX_REPO_MODE, DEVBOX_REPO_DIR, DEVBOX_REPO_URL.
set -euo pipefail

case "${DEVBOX_REPO_MODE:-none}" in
  clone)
    if [ ! -d "$HOME/$DEVBOX_REPO_DIR" ]; then
      jj git clone "$DEVBOX_REPO_URL" "$HOME/$DEVBOX_REPO_DIR"
    fi
    ;;
  push)
    if [ ! -d "$HOME/$DEVBOX_REPO_DIR" ]; then
      git init --initial-branch=main "$HOME/$DEVBOX_REPO_DIR"
      (cd "$HOME/$DEVBOX_REPO_DIR" \
        && jj git init --colocate \
        && git config receive.denyCurrentBranch updateInstead)
    fi
    ;;
  none) ;;
  *)
    echo "unknown DEVBOX_REPO_MODE: ${DEVBOX_REPO_MODE:-}" >&2
    exit 1
    ;;
esac
