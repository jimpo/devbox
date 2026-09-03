# devbox: Claude Code for the agent account - the binary from the store, and the
# account's own ~/.claude for everything Claude Code writes there itself.
#
# THE PACKAGE IS THE BINARY THE INSTALLER FETCHES. nixpkgs' `claude-code`
# downloads the same native build from downloads.claude.ai that
# `claude.ai/install.sh` does, at the version its manifest.json records, and
# wraps it. The version therefore moves with the nixpkgs pin in flake.nix rather
# than with whatever the installer served that day. It is an unfree package;
# options.nix allows unfree for the whole flake.
#
# SELF-UPDATE IS OFF, and by the package rather than by luck: its wrapper sets
# DISABLE_AUTOUPDATER=1 with makeBinaryWrapper's `--set`, which forces the value
# instead of defaulting it, so no environment can switch the updater back on.
# That matters here because the binary lives in the read-only store, where an
# update can only fail. DISABLE_INSTALLATION_CHECKS=1 is set the same way and is
# what stops it asking to be reinstalled by the native installer. Plugins are a
# separate matter: FORCE_AUTOUPDATE_PLUGINS is a `--set-default`, and plugins
# live under ~/.claude, which is writable.
#
# THE PROFILE'S `claude` IS THE ONE THAT RUNS: the driver's ~/.bash_env puts
# ~/.nix-profile/bin ahead of ~/.local/bin, so an account that still holds a
# binary there reaches this one first.
#
# WHY THE CONFIGURATION BELOW IS IMPERATIVE - do not "fix" it into `home.file`.
# Claude Code writes its own configuration: ~/.claude/settings.json is what
# `/config` and friends edit, and ~/.claude.json is rewritten constantly.
# home-manager owns a file by symlinking it read-only into the store, so a
# `home.file` entry for either path would make those writes fail, or have Claude
# replace the symlink with a plain file - which the NEXT activation refuses,
# because it is a file the generation wants and did not write
# (home-manager#4947). home-manager's own `programs.claude-code.settings` writes
# exactly that symlink, which is why the package goes into the profile through
# `home.packages` and the one setting devbox needs is MERGED into whatever the
# file already holds.
#
# The merge is a `home.activation` entry, so the whole mod stays under one name
# and inside one `home-manager switch` (a playbook name resolves to a script or
# to a module, never both). Activation is the generation's own imperative phase:
# it runs as the agent, after the write boundary, on every switch, which is what
# makes a reconverge a no-op rather than a second install.
{ config, lib, pkgs, ... }:

let
  settingsFile = "${config.devbox.workspace.homeDirectory}/.claude/settings.json";

  # `bypassPermissions` is the permission mode that skips every approval prompt.
  # The box is a disposable, isolated VM and the agent runs in an account with
  # no sudo, so the interactive prompt is pure friction.
  #
  # Every tool the script needs comes from the store, by name: an activation
  # script runs with the PATH home-manager builds for it, not the account's, so
  # nothing here may assume a binary the box happens to have. git is the one
  # that is not obvious - it is what `claude plugin marketplace add` clones the
  # marketplace with.
  configure = pkgs.writeShellScript "devbox-claude-code-configure" ''
    set -eu
    export PATH="${lib.makeBinPath [ pkgs.coreutils pkgs.git ]}:$PATH"

    settings=${lib.escapeShellArg settingsFile}
    mkdir -p "$(dirname "$settings")"
    [ -f "$settings" ] || echo '{}' >"$settings"

    tmp="$(mktemp "$settings.XXXXXX")"
    if ! ${lib.getExe pkgs.jq} '.permissions.defaultMode = "bypassPermissions"' \
        "$settings" >"$tmp"; then
      rm -f "$tmp"
      echo "devbox: $settings is not valid JSON; fix or delete it, then re-run" \
           "devbox sync --mods=home-manager" >&2
      exit 1
    fi
    mv "$tmp" "$settings"

    # The plugin marketplace every workspace gets. `marketplace add` exits
    # non-zero once it is already registered, which is every reconverge, so the
    # failure is tolerated; a genuine first-run failure is visible in the
    # switch's output either way.
    ${lib.getExe pkgs.claude-code} plugin marketplace add \
      anthropics/claude-plugins-official || true
  '';
in
lib.mkIf config.devbox.mods.claude-code.enable {
  home.packages = [ pkgs.claude-code ];

  home.activation.devboxClaudeCode = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    run ${configure}
  '';
}
