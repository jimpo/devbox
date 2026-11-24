# devbox: codex - the OpenAI Codex CLI in the agent's home, from OpenAI's own
# standalone installer, plus the MCP servers other modules ask for. Opt-in:
# import `devbox.builtinModules.codex` (it is not in `default`).
#
# SELF-UPDATING, which is why it is the installer and not a package in the
# generation. codex knows how it was installed: for a standalone install
# `codex update` re-runs exactly the command below, and the TUI offers it when a
# newer release exists. ~/.local/bin/codex is a symlink into a release directory
# under ~/.codex, all of it the account's own, so the running binary can replace
# itself. The install is skipped once `codex` exists, so a reconverge needs no
# network for it.
#
# CODEX_NON_INTERACTIVE=1 because activation has no terminal: it makes the
# installer skip its prompts, including the offer to start codex when it
# finishes.
#
# PUTTING ~/.local/bin ON PATH FIRST is what keeps the installer out of the
# account's shell files. It appends an `export PATH=...` block to a shell
# profile only when its install directory is absent from PATH, and the agent's
# ~/.zshenv is a symlink into the read-only store. The generation puts that
# directory on PATH for every later shell (nix/zshenv.nix), so the block would
# be redundant as well as unwritable.
#
# SETTINGS are one nix attrset, `devbox.codex.settings`, merged across modules
# like mcpServers and written into ~/.codex/config.toml on every switch:
#
#   devbox.codex.settings.model = "gpt-5";
#
# The module itself sets `approval_policy = "never"` and `sandbox_mode =
# "danger-full-access"` (what `codex --yolo` sets), like claude-code's
# bypassPermissions: the box is a disposable VM and the agent has no sudo, so
# the prompts are pure friction. They are `mkDefault`, so a module can override
# them. The attrset is merged over the existing file (remarshal and jq), so what
# codex wrote itself, such as `[projects]` trust entries, stays; a key in the
# attrset wins over the same key in the file. Comments in the file are lost.
#
# Authentication is the account's: `codex login`, or an OPENAI_API_KEY in the
# workspace's `[env]`, which reaches the shell from the 0600
# ~/.config/devbox/env.
#
# MCP SERVERS go through `codex mcp add`, never ~/.codex/config.toml.
# `devbox.codex.mcpServers` merges across modules:
#
#   devbox.codex.mcpServers.github = {
#     url = "https://api.githubcopilot.com/mcp/";
#     bearerTokenEnvVar = "GITHUB_TOKEN";
#   };
#
# THE TOKEN IS NEVER IN THE CONFIG. Codex stores only the NAME of the variable
# and reads it from its environment (the 0600 ~/.config/devbox/env the account's
# ~/.zshenv sources) when it calls the server. A server dropped from the option
# stays registered: removal is `codex mcp remove`, by hand.
{ config, lib, pkgs, ... }:

let
  cfg = config.devbox.codex;

  addServer = name: s: ''
    run codex mcp remove ${lib.escapeShellArg name} 2>/dev/null || true
    run codex mcp add ${lib.escapeShellArg name} --url ${lib.escapeShellArg s.url}${
      lib.optionalString (s.bearerTokenEnvVar != null)
        " --bearer-token-env-var ${lib.escapeShellArg s.bearerTokenEnvVar}"}
  '';

  json = pkgs.formats.json { };
  settingsFile = json.generate "codex-settings.json" cfg.settings;
in
{
  options.devbox.codex.settings = lib.mkOption {
    description = ''
      Settings merged into ~/.codex/config.toml on every switch. Any TOML
      value, so tables are nested attrsets.
    '';
    default = { };
    type = json.type;
  };

  config.devbox.codex.settings = {
    approval_policy = lib.mkDefault "never";
    sandbox_mode = lib.mkDefault "danger-full-access";
  };

  options.devbox.codex.mcpServers = lib.mkOption {
    description = ''
      Streamable-HTTP MCP servers registered with `codex mcp add`. Re-added on
      every switch (`mcp add` on an existing name is an error, so it is removed
      first), which is what lets a URL or variable change.
    '';
    default = { };
    type = lib.types.attrsOf (lib.types.submodule {
      options = {
        url = lib.mkOption { type = lib.types.str; };
        bearerTokenEnvVar = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Name of the environment variable holding the bearer token.";
        };
      };
    });
  };

  config.home.activation.devboxCodex = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    # Activation starts with a minimal PATH: the installer needs curl and sh,
    # the settings merge needs jq and remarshal.
    export PATH="${lib.makeBinPath [ pkgs.curl pkgs.coreutils pkgs.jq pkgs.remarshal ]}:${config.home.homeDirectory}/.local/bin:$PATH"

    command -v codex >/dev/null \
      || run sh -c 'curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh'

    cfgfile="$HOME/.codex/config.toml"
    run mkdir -p "$HOME/.codex"
    run touch "$cfgfile"
    tmp="$(mktemp)"
    toml2json "$cfgfile" | jq --slurpfile s ${settingsFile} '. * $s[0]' | json2toml >"$tmp" \
      && run mv "$tmp" "$cfgfile"

    ${lib.concatStringsSep "\n" (lib.mapAttrsToList addServer cfg.mcpServers)}
  '';
}
