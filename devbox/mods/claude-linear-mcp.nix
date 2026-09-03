# devbox: Linear's MCP server, registered with Claude Code in the agent
# account. Opt-in - a workspace names `claude-linear-mcp` in its own `mods`.
#
# TWO MOD NAMES, TWO MODULES. This one and `claude-github-mcp` stay apart
# because a workspace picks its servers one at a time: a config that wants
# Linear and not GitHub says so by naming one mod, and each name carries its own
# `devbox.mods.<name>.enable` flag.
#
# WHY THIS IS IMPERATIVE - do not "fix" it into `home.file`. An MCP server is
# registered by `claude mcp add`, an imperative command writing ~/.claude.json,
# a file Claude Code rewrites constantly. A `home.file` entry for that path
# would be a read-only symlink into the store: Claude's own writes would fail,
# or would replace the symlink and leave the NEXT activation refusing a file it
# did not write (home-manager#4947). So the registration is a `home.activation`
# entry - the generation's own imperative phase, running as the agent on every
# switch, which is what makes a reconverge a no-op.
#
# THE API KEY IS NEVER RESOLVED HERE. The header below carries the literal,
# UNEXPANDED string `${LINEAR_API_KEY}`, which Claude Code resolves from the
# environment at call time - and that environment gets it from the 0600
# ~/.config/devbox/env the agent's ~/.zshenv sources. /nix/store is
# world-readable and shared by every attachment on the box, so a value from a
# workspace's `[env]` may never appear in a nix expression; only the name of the
# variable does.
#
# `''${...}` IS THE NIX ESCAPE, and it is load-bearing: inside an indented
# string `''${` is a literal dollar-brace, while a bare `${` would open a nix
# antiquotation and this file would fail to evaluate (or, worse, interpolate).
# The single quotes around the header keep the shell from expanding it in turn,
# so what reaches `claude mcp add` is the two words and the brace form.
#
# THE BINARY IS NAMED FROM THE STORE rather than looked up on a PATH, because an
# activation script runs with the PATH home-manager builds for it. `mcp add`
# only edits this account's own config file, so the registration lands whichever
# half of the resolution rule provided the `claude` the account then runs.
{ config, lib, pkgs, ... }:

let
  register = pkgs.writeShellScript "devbox-claude-linear-mcp" ''
    set -eu
    claude=${lib.getExe pkgs.claude-code}

    # Removing first is what lets the URL or the header change: `mcp add` on a
    # name that is already registered is an error, and this runs on every
    # switch.
    "$claude" mcp remove --scope user linear >/dev/null 2>&1 || true

    # SC2016 is the point: the header is stored literally and Claude Code
    # expands it from the environment when it calls the server.
    # shellcheck disable=SC2016
    "$claude" mcp add --scope user --transport http linear \
      https://mcp.linear.app/mcp \
      --header 'Authorization: Bearer ''${LINEAR_API_KEY}'
  '';
in
lib.mkIf config.devbox.mods.claude-linear-mcp.enable {
  home.activation.devboxClaudeLinearMcp =
    lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${register}
    '';
}
