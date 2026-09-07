#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# packages = ["zsh", "vim", "git", "curl", "ca-certificates", "rsync", "unzip",
#             "build-essential", "pkg-config"]
# ///
#
# mod: box-packages - the apt packages that genuinely have to be the machine's
# rather than one account's.
#
# BOX-LEVEL: apt is the machine's, so this runs once per box rather than once
# per attachment. Everything an agent's tooling needs comes out of its
# home-manager generation instead, in its own profile and nowhere else; what is
# left here is the short list that a nix profile cannot serve:
#
#   * `rsync` is the receiver for `devbox sync`, which is a NON-interactive ssh
#     session - exactly the PATH a nix profile binary is not reliably on.
#   * `zsh` is every agent account's login shell, and a login shell has to be a
#     path that outlives a garbage collection: `chsh` to a store path lets a GC
#     delete the shell out from under the account.
#   * `vim` is the privileged account's editor. That account has no generation
#     by design (it is the box's debugging entry point), so `devbox box ssh`
#     needs an apt one.
#   * the rest - `git`, `curl`, `ca-certificates`, `unzip`, `build-essential`,
#     `pkg-config` - are what a mod body, an installer or a native build reaches
#     for before any generation exists, including on the very first run.
#
# THERE IS NO BODY. The list is the whole mod: the driver apt-installs a mod's
# `packages` itself, as root, before running it. `runas = ["root"]` is still
# what says "system work", and it is load-bearing at box scope - scoped_runas
# drops an agent-only mod from a box run entirely, packages and all.
#
# The first-boot init script installs most of this too, so a new box starts
# with it. That is a head start, not a duplicate: user-data runs once and can
# never reach an existing box, and this mod reconverges - a package added here
# arrives on a box created last month with `devbox sync --mods`.
#
# Idempotency: apt install is a no-op when the packages are already present, so
# a reconverge is fast.
