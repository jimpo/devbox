# devbox: the OpenAI Codex CLI for the agent account - `pkgs.codex`, the rust
# build, at the version the nixpkgs pin in flake.nix records. Its wrapper puts
# ripgrep and bubblewrap on codex's own PATH, which is what its search and its
# Linux sandbox need.
#
# OPT-IN: a workspace names `codex` in its own `mods`. Claude Code is the base
# playbook's agent CLI; a second one is a closure every attachment would
# otherwise carry.
#
# AUTHENTICATION IS THE ACCOUNT'S, never the generation's. `codex login` writes
# ~/.codex/auth.json in the agent's home, and an OPENAI_API_KEY in the
# workspace's `[env]` reaches codex through the 0600 ~/.config/devbox/env that
# ~/.zshenv sources. Nothing about either goes into a nix expression: the store
# is world-readable and every attachment on the box shares it.
#
# ~/.codex STAYS CODEX'S. It writes auth.json, its history and its session
# records there, and config.toml is the account's own file - home-manager owns
# a file by symlinking it read-only into the store, so declaring any of them
# would break those writes. Approvals and the sandbox are therefore codex's
# defaults: set `approval_policy` / `sandbox_mode` in ~/.codex/config.toml on
# the box, or pass `--dangerously-bypass-approvals-and-sandbox` per run - the
# box is a disposable VM and the agent account has no sudo.
{ config, lib, pkgs, ... }:

lib.mkIf config.devbox.mods.codex.enable {
  home.packages = [ pkgs.codex ];
}
