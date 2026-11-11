# devbox: the option surface a module reads, and the bridge from the rendered
# `workspace.nix` into it.
#
# This is the ONE file that imports workspace.nix. A module reads
# `config.devbox.workspace.<value>` instead, so the shape of the rendered file stays devbox's business and a
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
  };

  config = {
    # Unfree packages are allowed, which both editors' plugin sets need:
    # nixpkgs' vim/neovim plugin updater stamps `meta.license = unfree` on
    # every plugin whose licence it could not detect - base16-vim and vim-go
    # among them - so neither `neovim.nix` nor configenv's vim evaluates
    # without this.
    #
    # It takes effect despite flake.nix handing `homeManagerConfiguration` a
    # `pkgs`: that only sets `nixpkgs.config` to a `mkDefault`, and
    # home-manager's nixpkgs module re-imports nixpkgs with whatever this
    # resolves to.
    nixpkgs.config.allowUnfree = true;

    devbox.workspace = {
      inherit (workspace) agentUser homeDirectory jjName jjEmail;
    };

    home.username = workspace.agentUser;
    home.homeDirectory = workspace.homeDirectory;

    # The home-manager release whose backwards-compatible defaults this
    # configuration expects - a compatibility marker, not a version to install.
    # Bump it deliberately, and only together with the pins in flake.nix.
    home.stateVersion = "26.05";
  };
}
