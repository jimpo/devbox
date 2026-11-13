# devbox: Claude Code in the agent's home, from the native installer, with every
# tool call auto-approved, the official plugin marketplace registered, and the
# MCP servers and plugins other modules ask for.
#
# THE INSTALLER'S BINARY KEEPS ITSELF CURRENT, which is why this is an
# activation step and not `programs.claude-code`: `claude.ai/install.sh` puts
# ~/.local/bin/claude in the account's own home with the auto-updater on, so a
# long-lived box follows Claude Code's releases between reconverges. A store
# path is read-only and could only hold the version its nixpkgs pin names. The
# install is skipped once `claude` exists, so a reconverge needs no network for
# it. Nothing in the generation provides a `claude`, so there is nothing to
# shadow it.
#
# SETTINGS STAY WRITABLE: `permissions.defaultMode = bypassPermissions` is
# merged into ~/.claude/settings.json with jq, leaving what `/config` wrote in a
# live session alone. The box is a disposable VM and the agent has no sudo, so
# the permission prompt is pure friction.
#
# MCP SERVERS AND PLUGINS go through the `claude` CLI, never the config files.
# `devbox.claudeCode.mcpServers` and `devbox.claudeCode.plugins` merge across
# modules, so a module that needs a plugin just sets it:
#
#   devbox.claudeCode.plugins = [ "rust-analyzer-lsp@claude-plugins-official" ];
#   devbox.claudeCode.mcpServers.github = {
#     url = "https://api.githubcopilot.com/mcp/";
#     headers.Authorization = "Bearer \${GITHUB_TOKEN}";
#   };
#
# THE TOKEN IS NEVER RESOLVED HERE. A header value is stored as written, so
# `\${GITHUB_TOKEN}` stays a literal that Claude Code expands from its
# environment (the 0600 ~/.config/devbox/env the account's ~/.zshenv sources)
# when it calls the server. The value is in no store path and no config.
#
# A server dropped from the option stays registered: removal is `claude mcp
# remove`, by hand.
{ config, lib, pkgs, ... }:

let
  cfg = config.devbox.claudeCode;

  addServer = name: s: ''
    run claude mcp remove --scope user ${lib.escapeShellArg name} 2>/dev/null || true
    run claude mcp add --scope user --transport ${lib.escapeShellArg s.transport} \
      ${lib.escapeShellArg name} ${lib.escapeShellArg s.url} ${
        lib.concatStringsSep " " (lib.mapAttrsToList
          (k: v: "--header ${lib.escapeShellArg "${k}: ${v}"}") s.headers)}
  '';
in
{
  options.devbox.claudeCode = {
    mcpServers = lib.mkOption {
      description = ''
        MCP servers registered at user scope with `claude mcp add`. Re-added on
        every switch (`mcp add` on an existing name is an error, so it is
        removed first), which is what lets a URL or header change.
      '';
      default = { };
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          url = lib.mkOption { type = lib.types.str; };
          transport = lib.mkOption {
            type = lib.types.enum [ "http" "sse" ];
            default = "http";
          };
          headers = lib.mkOption {
            type = lib.types.attrsOf lib.types.str;
            default = { };
            description = "Sent as `Name: value`; write a secret as a literal `\${VAR}`.";
          };
        };
      });
    };

    plugins = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "rust-analyzer-lsp@claude-plugins-official" ];
      description = "Plugins (`name@marketplace`) installed with `claude plugin install`.";
    };
  };

  config.home.activation.devboxClaudeCode = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    # Activation starts with a minimal PATH: the installer needs curl and bash,
    # the marketplace clone needs git, and the settings merge needs jq.
    export PATH="${lib.makeBinPath [ pkgs.curl pkgs.bash pkgs.coreutils pkgs.git pkgs.jq ]}:${config.home.homeDirectory}/.local/bin:$PATH"

    command -v claude >/dev/null \
      || run bash -c 'curl -fsSL https://claude.ai/install.sh | bash'

    # Exits non-zero once registered, which is every reconverge.
    run claude plugin marketplace add anthropics/claude-plugins-official || true

    settings="$HOME/.claude/settings.json"
    run mkdir -p "$HOME/.claude"
    [ -f "$settings" ] || run bash -c 'echo "{}" > "$1"' _ "$settings"
    tmp="$(mktemp)"
    jq '.permissions.defaultMode = "bypassPermissions"' "$settings" >"$tmp" \
      && run mv "$tmp" "$settings"

    ${lib.concatStringsSep "\n" (lib.mapAttrsToList addServer cfg.mcpServers)}
    ${lib.concatMapStringsSep "\n" (p: "run claude plugin install ${lib.escapeShellArg p}") cfg.plugins}
  '';
}
