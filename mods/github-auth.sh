#!/usr/bin/env bash
#
# mod: github-auth - wire git/jj HTTPS auth for github.com to a fine-grained PAT.
# `gh` reads GITHUB_TOKEN/GH_TOKEN from the environment natively (no `gh auth
# login`, so nothing lands in ~/.config/gh/hosts.yml); this just points git's
# credential helper at gh, so `git`/`jj git` HTTPS - including repo-bootstrap's
# private `jj git clone` - authenticate transparently. No-op when GITHUB_TOKEN
# is unset (public clones work unauthenticated).
#
# Context (from the environment): GITHUB_TOKEN, from the box's [env] file
# (~/.config/devbox/env), which the driver sources before running the mods.
set -euo pipefail

if [ -n "${GITHUB_TOKEN:-}" ]; then
  gh auth setup-git
fi
