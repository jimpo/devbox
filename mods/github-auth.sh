#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# ///
#
# mod: github-auth - wire git/jj HTTPS auth for github.com to a fine-grained PAT.
# `gh` reads GITHUB_TOKEN/GH_TOKEN from the environment natively (no `gh auth
# login`, so nothing lands in ~/.config/gh/hosts.yml); this points git's
# credential helper at gh, so `git`/`jj git` HTTPS - including repo-bootstrap's
# private `jj git clone` - authenticate transparently. The clone URL stays plain
# HTTPS: gh is the credential helper behind it, not the transport.
#
# The helper line lives in /etc/gitconfig, box-wide, and holds no secret: it
# names a helper, and the helper answers with whatever GITHUB_TOKEN the *calling*
# account has in its own 0600 ~/.config/devbox/env. One system-wide line is
# therefore right for every account on the box, the privileged one included.
# /etc is also the one place `dotfiles` cannot reach - it copies configenv's own
# gitconfig over ~/.gitconfig, which erases any helper written there - so this
# mod carries no ordering constraint at all.
#
# Unconditional: with no token the helper prints nothing and exits 1, git falls
# through, and public clones are unaffected - so a box needs no re-provisioning
# when a workspace later gains a token.
#
# Idempotency: `git config --replace-all` collapses the key to one value.
set -euo pipefail

gh_path="$(command -v gh)" || {
  echo "github-auth: gh is not installed; it is a base package from the init script" >&2
  exit 1
}

# Written as gh's own `gh auth setup-git` writes it: the leading `!` is git's
# "run this through the shell", which is what carries the two arguments.
git config --file /etc/gitconfig --replace-all \
  'credential.https://github.com.helper' "!$gh_path auth git-credential"
