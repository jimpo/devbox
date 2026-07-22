#!/usr/bin/env bash
#
# mod: github-auth - wire up `gh` and git/jj HTTPS auth for github.com from a
# fine-grained PAT. No-op when the token is unset (cloning a public repo works
# unauthenticated). Ordered before repo-bootstrap so a private `jj git clone`
# can authenticate.
#
# Context (from the environment): DEVBOX_GITHUB_TOKEN (secret; sourced by the
# driver from a mode-0600 file). The token is only ever fed to `gh` on stdin -
# never echoed, never written to disk - and the auth step runs in a `set +x`
# subshell so even a traced run can't leak it.
set -euo pipefail

if [ -n "${DEVBOX_GITHUB_TOKEN:-}" ]; then
  ( set +x; printf '%s\n' "$DEVBOX_GITHUB_TOKEN" | gh auth login --with-token )
  gh auth setup-git
fi
