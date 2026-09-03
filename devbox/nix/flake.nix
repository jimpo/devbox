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
# CONVERTING A MOD, end to end. Three steps, and the third is "nothing":
#
#   1. Write `modules/<mod-name>.nix`, named exactly as the bash mod it
#      replaces (see the shape below).
#   2. DELETE `mods/<mod-name>.sh`, in the same commit. A script wins over a
#      module of the same name, so while it exists the bash mod is what runs
#      and the new module is inert.
#   3. Leave `WORKSPACE_BASE_MODS` (provision.py) ALONE. The name stays in the
#      playbook: that tuple lists what an attachment gets, not which half
#      provides it, and devbox resolves each name by file presence -
#      `<name>.sh` in any mods directory, else `<name>.nix` in any
#      nix-modules directory. Removing the name would turn the module off.
#      The same goes for a mod a workspace names in its own `mods = [...]`:
#      that config keeps working untouched.
#
# THIS FILE NEVER LISTS MODULES BY NAME. `modules` below is every `.nix` file
# `readDir` finds directly under `./modules` - devbox writes one payload
# `nix/modules/<name>.nix` per name it resolves (built-in, global, or a
# workspace's own - see provision.find_nix_module), whatever the source, and
# this flake just imports whatever landed there. That is what makes a
# workspace's or a user's global `<mods dir>/<name>.nix` work with zero
# changes here: it never has to be devbox's own repo, and it never has to be
# in a directory shaped like this one. `provision.py`'s
# `NIX_MODULES_DIR` is the payload's `modules/` before it travels, so a
# built-in conversion is still "write the file, delete the script" - there is
# just no line to add to a list any more.
#
# Two consequences worth knowing. A mod that `requires` a converted one still
# validates - a module is not a step in the run, so it satisfies a dependency
# wherever it sits. And a converted name is no longer in the driver's manifest,
# so `devbox sync --mods=<name>` no longer knows it: the way to reconverge a
# converted mod is `devbox sync --mods=home-manager`, which re-switches the
# whole generation.
#
# `devbox.mods.<name>.enable` MEANS "the generation provides this mod". devbox
# renders one flag per resolved module name (options.nix declares them
# free-form), so:
#
#   * a mod the workspace's playbook names, with no `<name>.sh` anywhere -> true
#   * a mod the workspace never asked for                                -> false
#   * a mod some `<name>.sh` still answers to (a workspace shadowing a
#     built-in with its own script, or step 2 not done yet)              -> false
#
# The flag is never true at the same time as a bash mod of that name running:
# one name, one implementation. A module READS its own flag and never declares
# it. A module that needs more than a boolean declares its own options in its
# own namespace (`options.devbox.<name>.<option>`), outside `devbox.mods`.
#
# Shape:
#
#     { config, lib, pkgs, ... }:
#     lib.mkIf config.devbox.mods.<mod-name>.enable {
#       home.packages = [ pkgs.<package> ];
#     }
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
# WORKSPACE-LOCAL AND GLOBAL .nix MODULES ARE SUPPORTED, same
# workspace > global > built-in precedence as a bash mod, and out of the same
# directory: a workspace's own `<mods dir>/<name>.nix` (beside its `<name>.sh`
# scripts, either marker spelling - see config.Marker) or a user's global
# `~/.config/devbox/mods/<name>.nix` resolves exactly like devbox's own
# built-in one, or shadows it if both exist. Only the built-ins live in a
# `modules/` of their own, and only because it is THIS directory - the flake
# devbox ships, which `nix flake check` can evaluate from the source tree. The
# outer two levels have no flake of their own to sit in and so gain nothing
# from a separate directory. A module needing sibling files of its own (its
# own lib, say) isn't supported yet - every shipped module today is one
# self-contained file, and only `readDir`-discovered `.nix` files travel in
# the payload.
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
      inherit (nixpkgs) lib;

      # A box is x86_64 (libvirt, most EC2/Lightsail) or aarch64 (Graviton),
      # and devbox cannot know which from the host, so the configuration is
      # built for both and `mods/home-manager.sh` picks by `uname -m`. That is
      # why the attribute name carries the system: `pkgs` for the wrong one
      # fails at evaluation, and a single `agent` could only ever guess.
      systems = [ "aarch64-linux" "x86_64-linux" ];

      # Every mod this payload ships a module for, whatever put it here -
      # devbox's own built-in, a user's global config, or this workspace's
      # own (see provision.nix_payload_files) - discovered rather than named,
      # which is what lets a module live outside devbox's own repo. `readDir`
      # on `./modules` is pure: by the time this evaluates, the flake (this
      # whole directory) is already a realized store path (`path:` copies it
      # there - see the note above), so this reads no more of the filesystem
      # than importing a hardcoded list would.
      moduleFiles = lib.filterAttrs (name: type: type == "regular" && lib.hasSuffix ".nix" name)
        (builtins.readDir ./modules);
      discoveredModules = map (name: ./modules + "/${name}") (builtins.attrNames moduleFiles);

      mkAgent = system: home-manager.lib.homeManagerConfiguration {
        pkgs = nixpkgs.legacyPackages.${system};
        modules = [
          # The option surface, and the rendered workspace values.
          ./options.nix
          # devbox's own shell plumbing: the agent's ~/.zshenv. Not a mod, so
          # it is always in the generation and reads no `devbox.mods` flag.
          ./zshenv.nix
        ] ++ discoveredModules;
      };
    in
    {
      homeConfigurations = builtins.listToAttrs (map
        (system: { name = "agent-${system}"; value = mkAgent system; })
        systems);
    };
}
