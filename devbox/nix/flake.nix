# devbox: the agent account's declarative home environment.
#
# One flake per attachment. It ships in the provisioning payload next to
# `mods/`, and `mods/home-manager.sh` switches the agent account to it on the
# box.
#
# WHAT SITS BESIDE THIS FILE DEPENDS ON WHERE YOU ARE LOOKING. In devbox's own
# repo this directory is a SKELETON - flake.nix, options.nix, zshenv.nix - and
# it cannot be evaluated: options.nix imports a `workspace.nix` that does not
# exist yet, and there is no `modules/` at all. Both appear only in an
# assembled payload, where devbox renders `workspace.nix` per attachment
# (provision.render_workspace_nix) and writes one `modules/<name>.nix` per
# module it resolved, out of the same mods directories the bash mods come from
# (provision.nix_payload_files). `devbox nix check` assembles one on the host
# and runs nix against it; that is the only way to evaluate a module without a
# box.
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
# NO flake.lock. `nixpkgs` and `home-manager` are pinned to an exact
# 40-character rev instead, which makes the lock nix writes on the box
# deterministic for them. To bump them, resolve the two branch heads and paste
# the revs below (the nixpkgs release and the home-manager branch must match):
#
#   curl -s https://api.github.com/repos/NixOS/nixpkgs/branches/nixos-26.05
#   curl -s https://api.github.com/repos/nix-community/home-manager/branches/release-26.05
#
# `mods/home-manager.sh` pins its `nix run` of the home-manager CLI to the same
# rev as the input below; bump both together.
#
# `configenv` IS THE EXCEPTION: it rolls with its own `main`, deliberately, so
# an edit to the shared configs reaches every box without a devbox release.
# `mods/home-manager.sh` passes `--refresh` so a switch resolves that branch
# afresh rather than reusing nix's cached answer for up to `tarball-ttl`.
#
# ---- MODULE CONVENTIONS (read this before adding a module) ------------------
#
# CONVERTING A MOD, end to end. Three steps, and the third is "nothing". All
# of it happens in ONE directory - `devbox/mods/` for a built-in, a workspace's
# own mods directory for its own, `~/.config/devbox/mods/` for a global one -
# because that is where both halves of a mod live:
#
#   1. Write `<mod-name>.nix` beside the script, named exactly as it (see the
#      shape below).
#   2. DELETE `<mod-name>.sh`, in the same commit. A script wins over a module
#      of the same name, so while it exists the bash mod is what runs and the
#      new module is inert.
#   3. Leave `WORKSPACE_BASE_MODS` (provision.py) ALONE. The name stays in the
#      playbook: that tuple lists what an attachment gets, not which half
#      provides it, and devbox resolves each name by file presence -
#      `<name>.sh` in a mods directory, else `<name>.nix` in the same
#      directories. Removing the name would turn the module off. The same goes
#      for a mod a workspace names in its own `mods = [...]`: that config keeps
#      working untouched.
#
# Then run `devbox nix check` before you commit it. Nothing else on a host
# evaluates a module.
#
# THE OTHER DIRECTION IS THE SAME MOVE MIRRORED - write `<name>.sh`, delete
# `<name>.nix`, leave the playbook tuples alone - and `claude-code` and `codex`
# are deliberately on that side of the line. Both install a binary into
# ~/.local/bin that replaces ITSELF as new versions ship, which a read-only
# store path cannot do; a package would pin each to the version this file's
# nixpkgs rev happens to name. Their MCP companions (`claude-github-mcp`,
# `claude-linear-mcp`) are scripts for a plainer reason: they drive the
# `claude` those installers put in the account's own home.
#
# THIS FILE NEVER LISTS MODULES BY NAME. `modules` below is every `.nix` file
# `readDir` finds directly under `./modules`, which exists only in an assembled
# payload - devbox writes one file there per name it resolves (built-in,
# global, or a workspace's own - see provision.find_nix_module), whatever
# directory it came from, and this flake imports whatever landed. That is what
# lets a mod's two halves live side by side in one directory at every level,
# and what makes a workspace's or a user's global `<mods dir>/<name>.nix` work
# with zero changes here: a module never has to be in devbox's own repo, and
# never has to sit in a directory shaped like a flake.
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
# EXCEPT WHERE ANOTHER MODULE ALREADY OWNS THE FILE. Two modules that define
# the same `home.file` entry are an evaluation conflict, and a module that
# claims a path some bash mod writes on the box is a switch that aborts on its
# own stale `<file>.devbox-backup` the second time round. `dotfiles.nix`
# imports configenv, which owns ~/.zshrc (through `programs.zsh`), ~/.vimrc,
# ~/.ideavimrc, ~/.tmux.conf, ~/.gitconfig, ~/.config/jj/config.toml and
# ~/.config/nvim; devbox's own zshenv.nix owns ~/.zshenv through the same
# `programs.zsh`. A module wanting one of those reaches for whatever keeps it
# off the path instead - a `programs.<name>` option that writes elsewhere
# (`neovim.sideloadInitLua`), a config directory the program reads in addition
# (`jj.nix`'s conf.d), or plain `home.packages` with no `programs.<name>` at
# all. `github-auth.nix` is the same rule read forwards: it claims
# ~/.config/git/config precisely because configenv owns ~/.gitconfig.
#
# THE PROFILE IS THE SAME PROBLEM ONE LEVEL DOWN. `home.packages` is a merged
# list no module can take an entry out of, and `buildEnv` refuses two paths
# that own one file - two packages both providing bin/vim, say. A priority
# settles it where the duplicate is unavoidable: every path gets 5 unless its
# meta says otherwise, so `lib.hiPrio` on the one that should win skips the
# other rather than colliding (`vim.nix`). NEITHER of these two failures is an
# evaluation error, so `devbox nix check` sees them only with `--build`.
#
# NO SECRETS, EVER. /nix/store is world-readable, on a box every attached
# workspace shares, so nothing from a workspace's `[env]` may appear in a nix
# expression - not as a value and not as a file an expression reads.
# Credentials stay in the 0600 ~/.config/devbox/env that `devbox sync
# --envvars` writes, and a mod that needs one reads it at run time.
#
# WORKSPACE-LOCAL AND GLOBAL .nix MODULES ARE SUPPORTED, same
# workspace > global > built-in precedence as a bash mod, and out of the same
# three directories (provision.mods_dirs): a workspace's own
# `<mods dir>/<name>.nix` (either marker spelling - see config.Marker), a
# user's global `~/.config/devbox/mods/<name>.nix`, and devbox's own
# `devbox/mods/<name>.nix`. Each resolves like the others and shadows the
# levels below it. A module needing sibling files of its own (its own lib,
# say) isn't supported yet - every shipped module today is one self-contained
# file, and only `readDir`-discovered `.nix` files travel in the payload.
{
  description = "devbox agent home environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/5dfba6236110080a54247d6460bc2ff5dda939cc";
    home-manager = {
      url = "github:nix-community/home-manager/65258d5c65a250189fde2e35f490d15e064c4c62";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Jim's shared configuration files, as a home-manager module -
    # `dotfiles.nix` imports it. `flake = false` because configenv uses
    # home-manager's non-flake interface: the input is the repository itself,
    # and `imports = [ configenv ]` resolves its default.nix. That also means
    # it carries no nixpkgs of its own to `follows`, so the box builds one
    # closure rather than two. Unpinned, on `main`, by decision - see the
    # flake.lock note above.
    configenv = {
      url = "github:jimpo/configenv";
      flake = false;
    };
  };

  outputs = { nixpkgs, home-manager, configenv, ... }:
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
        # A module reaches an input through its own argument list. `configenv`
        # is the only one worth handing over: it is a module a mod imports,
        # not a package set.
        extraSpecialArgs = { inherit configenv; };
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
