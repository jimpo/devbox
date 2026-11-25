# devbox: gemini - Google's Gemini CLI in the agent's home, installed with npm
# into the account's own prefix, plus the settings and MCP servers other modules
# ask for. Opt-in: import `devbox.builtinModules.gemini` (it is not in
# `default`).
#
# SELF-UPDATING, which is why it is `npm install -g` and not a package in the
# generation. Gemini CLI checks for a newer release at startup and, for a global
# npm install whose prefix the user can write, installs it in place. The prefix
# is ~/.npm-global (the same one `nodejs.nix` sets, repeated here so the module
# stands alone; identical values merge), so the running binary can replace
# itself. The install is skipped once `gemini` exists, so a reconverge needs no
# network for it.
#
# SETTINGS are one nix attrset, `devbox.gemini.settings`, merged across modules
# and into ~/.gemini/settings.json on every switch:
#
#   devbox.gemini.settings.ui.theme = "GitHub";
#
# The module itself sets `general.defaultApprovalMode = "yolo"`, like
# claude-code's bypassPermissions and codex's `--yolo`: the box is a disposable
# VM and the agent has no sudo, so the prompts are pure friction. It is
# `mkDefault`, so a module can override it. The attrset is merged over the
# existing file (jq), so what gemini wrote itself stays; a key in the attrset
# wins over the same key in the file.
#
# Authentication is the account's: `gemini` login, or a GEMINI_API_KEY in the
# workspace's `[env]`, which reaches the shell from the 0600
# ~/.config/devbox/env.
#
# MCP SERVERS are written into the same settings file (`mcpServers`):
#
#   devbox.gemini.mcpServers.github = {
#     url = "https://api.githubcopilot.com/mcp/";
#     headers.Authorization = "Bearer \${GITHUB_TOKEN}";
#   };
#
# THE TOKEN IS NEVER RESOLVED HERE. Gemini CLI expands `$VAR` / `${VAR}` in
# settings.json from its environment (the 0600 ~/.config/devbox/env the
# account's ~/.zshenv sources) when it starts, so the value is in no store path
# and no config. A server dropped from the option stays in the file until it is
# removed by hand.
{ config, lib, pkgs, ... }:

let
  cfg = config.devbox.gemini;

  json = pkgs.formats.json { };
  settingsFile = json.generate "gemini-settings.json" cfg.settings;
in
{
  options.devbox.gemini.settings = lib.mkOption {
    description = ''
      Settings merged into ~/.gemini/settings.json on every switch. Any JSON
      value, so nested sections are nested attrsets.
    '';
    default = { };
    type = json.type;
  };

  options.devbox.gemini.mcpServers = lib.mkOption {
    description = ''
      Streamable-HTTP MCP servers, written to `mcpServers` in
      ~/.gemini/settings.json on every switch.
    '';
    default = { };
    type = lib.types.attrsOf (lib.types.submodule {
      options = {
        url = lib.mkOption { type = lib.types.str; };
        headers = lib.mkOption {
          type = lib.types.attrsOf lib.types.str;
          default = { };
          description = "Sent with every request; write a secret as a literal `\${VAR}`.";
        };
      };
    });
  };

  config = {
    home.packages = [ pkgs.nodejs ];
    home.sessionVariables.NPM_CONFIG_PREFIX = "$HOME/.npm-global";
    home.sessionPath = [ "$HOME/.npm-global/bin" ];

    devbox.gemini.settings = {
      general.defaultApprovalMode = lib.mkDefault "yolo";
      mcpServers = lib.mapAttrs (_: s: { httpUrl = s.url; headers = s.headers; }) cfg.mcpServers;
    };

    home.activation.devboxGemini = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      # Activation starts with a minimal PATH: npm needs node, and the install
      # runs scripts that want a shell and coreutils.
      export NPM_CONFIG_PREFIX="${config.home.homeDirectory}/.npm-global"
      export PATH="${lib.makeBinPath [ pkgs.nodejs pkgs.coreutils pkgs.jq ]}:$NPM_CONFIG_PREFIX/bin:$PATH"

      command -v gemini >/dev/null \
        || run npm install -g @google/gemini-cli

      cfgfile="$HOME/.gemini/settings.json"
      run mkdir -p "$HOME/.gemini"
      [ -s "$cfgfile" ] || echo '{}' >"$cfgfile"
      tmp="$(mktemp)"
      jq --slurpfile s ${settingsFile} '. * $s[0]' "$cfgfile" >"$tmp" \
        && run mv "$tmp" "$cfgfile"
    '';
  };
}
