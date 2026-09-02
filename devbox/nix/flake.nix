# devbox: the agent account's declarative home environment.
#
# One flake per attachment. It ships in the provisioning payload next to
# `mods/`, and `mods/home-manager.sh` switches the agent account to it on the
# box. `workspace.nix` beside this file is rendered per attachment by devbox
# (provision.render_workspace_nix); everything else is static and shipped
# verbatim.
#
# HOME-MANAGER IS AGENT-ONLY. The box's privileged account keeps its apt zsh
# and vim, because that account is the machine's debugging entry point
# (`devbox box ssh`) and has to work whatever state a workspace's nix
# generation is in. The box playbook is bash + apt.
#
# THE FLAKE IS REFERRED TO AS `path:`, not as a bare path: a flake that sits
# inside a git work tree is evaluated from git and ignores untracked files, and
# the payload directory is not a repo. `path:` copies the directory to the
# store as it is on disk.
#
# NO flake.lock. Both inputs are pinned to an exact 40-character rev instead,
# which makes the lock nix writes on the box deterministic anyway. To bump
# them, resolve the two branch heads and paste the revs below (the nixpkgs
# release and the home-manager branch must match):
#
#   curl -s https://api.github.com/repos/NixOS/nixpkgs/branches/nixos-26.05
#   curl -s https://api.github.com/repos/nix-community/home-manager/branches/release-26.05
#
# `mods/home-manager.sh` pins its `nix run` of the home-manager CLI to the same
# rev as the input below; bump both together.
#
# ---- MODULE CONVENTIONS (read this before adding a module) ------------------
#
# One file per converted mod: `modules/<mod-name>.nix`, named exactly as the
# bash mod it replaces, and added to the `modules` list below - alphabetically,
# one entry per line.
#
# Shape:
#
#     { config, lib, pkgs, ... }:
#     lib.mkIf config.devbox.mods.<mod-name>.enable {
#       home.packages = [ pkgs.<package> ];
#     }
#
# `devbox.mods.<name>.enable` is declared generically in options.nix, so every
# mod in the workspace's playbook already has a flag: a module READS its own
# flag and never declares it. A module that needs more than a boolean declares
# its own options in its own namespace (`options.devbox.<name>.<option>`),
# outside the `devbox.mods` attribute set.
#
# The workspace's values are `config.devbox.workspace`: agentUser,
# homeDirectory, jjName, jjEmail. Read them from there, never by importing
# workspace.nix - options.nix is the one place that reads that file.
#
# A package goes into the profile with `home.packages`; a program's
# configuration goes through home-manager's own `programs.<name>`. A setting
# that is global rather than per-mod (`nixpkgs.config.allowUnfree`, say) goes
# in options.nix's `config` block, so there is one place to look for it.
#
# NO SECRETS, EVER. /nix/store is world-readable, on a box every attached
# workspace shares, so nothing from a workspace's `[env]` may appear in a nix
# expression - not as a value and not as a file an expression reads.
# Credentials stay in the 0600 ~/.config/devbox/env that `devbox sync
# --envvars` writes, and a mod that needs one reads it at run time.
#
# Workspace-local .nix modules are NOT supported. The workspace > global >
# built-in shadowing that bash mods have does not translate to the module
# system (`disabledModules`/`lib.mkForce` are clumsier than dropping a file
# into a directory). A workspace that needs something of its own ships a bash
# mod, exactly as it does today.
{
  description = "devbox agent home environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/5dfba6236110080a54247d6460bc2ff5dda939cc";
    home-manager = {
      url = "github:nix-community/home-manager/65258d5c65a250189fde2e35f490d15e064c4c62";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, ... }:
    let
      # A box is x86_64 (libvirt, most EC2/Lightsail) or aarch64 (Graviton),
      # and devbox cannot know which from the host, so the configuration is
      # built for both and `mods/home-manager.sh` picks by `uname -m`. That is
      # why the attribute name carries the system: `pkgs` for the wrong one
      # fails at evaluation, and a single `agent` could only ever guess.
      systems = [ "aarch64-linux" "x86_64-linux" ];

      mkAgent = system: home-manager.lib.homeManagerConfiguration {
        pkgs = nixpkgs.legacyPackages.${system};
        modules = [
          # The option surface, and the rendered workspace values.
          ./options.nix
          # Converted mods, alphabetically, one entry per line.
        ];
      };
    in
    {
      homeConfigurations = builtins.listToAttrs (map
        (system: { name = "agent-${system}"; value = mkAgent system; })
        systems);
    };
}
