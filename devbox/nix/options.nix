# devbox: the option surface a converted mod's module reads, and the bridge
# from the rendered `workspace.nix` into it.
#
# This is the ONE file that imports workspace.nix. A module reads
# `config.devbox.workspace.<value>` and `config.devbox.mods.<name>.enable`
# instead, so the shape of the rendered file stays devbox's business and a
# module keeps working if it changes.
{ lib, ... }:

let
  # Rendered per attachment by devbox (provision.render_workspace_nix) and
  # written into this directory as part of the provisioning payload. A plain
  # attribute set of data; it carries no credentials.
  workspace = import ./workspace.nix;
in
{
  options.devbox = {
    workspace = {
      agentUser = lib.mkOption {
        type = lib.types.str;
        description = "The unprivileged account this attachment lives in.";
      };
      homeDirectory = lib.mkOption {
        type = lib.types.str;
        description = "That account's home directory.";
      };
      jjName = lib.mkOption {
        type = lib.types.str;
        description = "The jj/git author name for this workspace's checkouts.";
      };
      jjEmail = lib.mkOption {
        type = lib.types.str;
        description = "The jj/git author email for this workspace's checkouts.";
      };
    };

    mods = lib.mkOption {
      # Free-form in the NAME, declared in the shape: devbox renders one entry
      # per mod in the workspace's playbook, including the mods that have no
      # module yet, so the flag has to exist for a name no file mentions. A
      # module reads its own flag; nothing declares one.
      type = lib.types.attrsOf (lib.types.submodule {
        options.enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Whether this mod is in the workspace's playbook.";
        };
      });
      default = { };
      description = ''
        One entry per mod in this workspace's playbook, as
        `<name>.enable`. Rendered from the workspace's effective `mods` list;
        a mod that has no module here is simply an inert flag.
      '';
    };
  };

  config = {
    devbox.workspace = {
      inherit (workspace) agentUser homeDirectory jjName jjEmail;
    };
    devbox.mods = workspace.mods;

    home.username = workspace.agentUser;
    home.homeDirectory = workspace.homeDirectory;

    # The home-manager release whose backwards-compatible defaults this
    # configuration expects - a compatibility marker, not a version to install.
    # Bump it deliberately, and only together with the pins in flake.nix.
    home.stateVersion = "26.05";
  };
}
