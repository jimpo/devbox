#!/usr/bin/env bash
#
# /// mod
# requires = ["jj", "github-auth"]
# ///
#
# mod: repo-bootstrap - get the workspace's checkouts onto the box, one per
# [[repos]] entry. "clone" pulls an existing repo via jj; "push" leaves an empty
# colocated jj+git repo ready to receive `jj git push` from the host. Ordered
# last in the base playbook (after jj + github-auth, so a private clone can
# authenticate).
#
# Every checkout lands in this one account, which is what makes them share a set
# of git/jj credentials - the point of putting several repos in one workspace.
#
# Idempotency: guarded per target directory, so a reconverge is a no-op for the
# repos that already exist and still creates the ones that don't.
#
# Context: the payload's `repos` file, one `<name>\t<mode>\t<dir>\t<url>` record
# per checkout, reached through DEVBOX_PAYLOAD_DIR (the driver exports it).
set -euo pipefail

repos="${DEVBOX_PAYLOAD_DIR:?repo-bootstrap needs DEVBOX_PAYLOAD_DIR}/repos"
[ -f "$repos" ] || { echo "payload has no repos file at $repos" >&2; exit 1; }

while IFS=$'\t' read -r name mode dir url; do
  [ -n "$name" ] || continue
  [ -d "$HOME/$dir" ] && continue
  case "$mode" in
    clone)
      # </dev/null: this loop is reading the repos file on stdin, and jj must not
      # consume the records still queued behind the current one.
      jj git clone "$url" "$HOME/$dir" </dev/null
      ;;
    push)
      git init --initial-branch=main "$HOME/$dir" </dev/null
      (cd "$HOME/$dir" \
        && jj git init --colocate \
        && git config receive.denyCurrentBranch updateInstead) </dev/null
      ;;
    *)
      echo "unknown mode '$mode' for repo '$name'" >&2
      exit 1
      ;;
  esac
done < "$repos"
