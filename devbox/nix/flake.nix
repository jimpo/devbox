# devbox: the agent account's declarative home environment.
#
# One flake per attachment. It ships in the provisioning payload next to
# `mods/`, and `mods/home-manager.sh` switches the agent account to it on the
# box.
#
# WHAT SITS BESIDE THIS FILE DEPENDS ON WHERE YOU ARE LOOKING. In devbox's own
# repo this directory is a SKELETON - flake.nix, options.nix, zshenv.nix - and
# it cannot be evaluated: options.nix imports a `workspace.nix` that does not
# exist yet. An assembled payload (provision.nix_payload_files) adds:
#
#   workspace.nix     rendered per attachment: agent user, home, jj identity
#   devbox-modules/   devbox's own modules (devbox/modules/)
#   my-modules/       the user's shared modules (~/.config/devbox/modules/)
#   workspace/        the workspace's default.nix and its sibling .nix files
#
# `devbox nix check` assembles one on the host and runs nix against it.
#
# HOME-MANAGER IS AGENT-ONLY. The box's privileged account keeps its distro zsh
# and vim, because that account is the machine's debugging entry point
# (`devbox box ssh`) and has to work whatever state a workspace's nix
# generation is in. The box playbook is bash + dnf.
#
# THE FLAKE IS REFERRED TO AS `path:`, not as a bare path: a flake that sits
# inside a git work tree is evaluated from git and ignores untracked files, and
# the payload directory is not a repo. `path:` copies the directory to the
# store as it is on disk.
#
# NO flake.lock, and none is wanted: a lock pins EVERY input, `configenv`
# included, and that is the one input meant to roll (see below) - a locked
# payload would freeze the branch `--refresh` exists to re-resolve, and the
# payload directory is read-only on the box, so nothing there could update it.
# `nixpkgs` and `home-manager` are pinned to an exact 40-character rev instead,
# which makes the lock nix writes on the box deterministic for them. To bump
# them, resolve the two branch heads and paste the revs below (the nixpkgs
# release and the home-manager branch must match):
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
# A MODULE IS A PLAIN HOME-MANAGER MODULE: importing it turns it on, and there
# are no devbox enable flags. Every module gets a `devbox` argument holding two
# attrsets of importable modules keyed by name (`<name>.nix`, or
# `<name>/default.nix`). It is a module argument, not `config.devbox`:
#
#   devbox.builtinModules   devbox/modules/             e.g. .neovim
#   devbox.globalModules    ~/.config/devbox/modules/   shared across workspaces
#
# The generation is options.nix, zshenv.nix, `jj` and `github-auth` (always:
# repo-bootstrap clones with them), plus the workspace's own `default.nix` -
# or `devbox.builtinModules.default`, the base profile, when it has none:
#
#     { devbox, pkgs, ... }: {
#       imports = with devbox; [
#         builtinModules.default builtinModules.uv globalModules.rust
#       ];
#       home.packages = [ pkgs.htop ];
#     }
#
# A workspace's default.nix may import sibling `.nix` files (`./extra.nix`);
# nothing else beside it travels to the box. A path outside the payload (an
# absolute host path) evaluates on the host and fails on the box.
#
# The workspace's values are `config.devbox.workspace`: agentUser,
# homeDirectory, jjName, jjEmail. A setting that is global rather than
# per-module (`nixpkgs.config.allowUnfree`) goes in options.nix.
#
# ONE OWNER PER FILE. Two modules defining the same `home.file` entry are an
# evaluation conflict. `dotfiles.nix` imports configenv, which owns ~/.zshrc
# (through `programs.zsh`), vim, ~/.vimrc, ~/.ideavimrc, ~/.tmux.conf,
# ~/.gitconfig, ~/.config/jj/config.toml and ~/.config/nvim; zshenv.nix owns
# ~/.zshenv through the same `programs.zsh`. A module wanting one of those
# reaches for whatever keeps it off the path - `neovim.sideloadInitLua`,
# jj's conf.d, ~/.config/git/config (github-auth.nix).
#
# THE PROFILE IS THE SAME PROBLEM ONE LEVEL DOWN: `buildEnv` refuses two
# packages owning one file; `lib.hiPrio` settles it. Neither failure is an
# evaluation error, so `devbox nix check` sees them only with `--build`.
#
# NO SECRETS, EVER. /nix/store is world-readable, on a box every attached
# workspace shares. Credentials stay in the 0600 ~/.config/devbox/env, and a
# module that needs one reads that file at run time.
#
# AGENT CLIs STAY SCRIPTS (`claude-code`, `codex`): their installers put a
# binary in ~/.local/bin that updates itself, which a store path cannot do.
{
  description = "devbox agent home environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/7dcdd87373d72e7a2173e4db02e20d3d7f495cfd";
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

      # `<name>.nix` and `<name>/default.nix` under `dir`, keyed by name; {}
      # when the payload has no such directory.
      modulesIn = dir:
        let
          entries = lib.filterAttrs (name: type:
            (type == "regular" && lib.hasSuffix ".nix" name)
            || (type == "directory" && builtins.pathExists (dir + "/${name}/default.nix")))
            (builtins.readDir dir);
        in
        if builtins.pathExists dir
        then lib.mapAttrs' (name: _: lib.nameValuePair (lib.removeSuffix ".nix" name) (dir + "/${name}")) entries
        else { };

      builtinModules = modulesIn ./devbox-modules;
      globalModules = modulesIn ./my-modules;

      workspaceModule =
        if builtins.pathExists ./workspace/default.nix
        then ./workspace/default.nix
        else builtinModules.default;

      # `devbox nix check` imports every module it can find, so a typo in one
      # no workspace uses still fails the check.
      checkModules = lib.optionals ((import ./workspace.nix).checkAll or false)
        (builtins.attrValues builtinModules ++ builtins.attrValues globalModules);

      mkAgent = system: home-manager.lib.homeManagerConfiguration {
        pkgs = nixpkgs.legacyPackages.${system};
        extraSpecialArgs = {
          inherit configenv;
          devbox = { inherit builtinModules globalModules; };
        };
        modules = [
          ./options.nix
          ./zshenv.nix
          builtinModules.jj
          builtinModules.github-auth
        ] ++ checkModules ++ [ workspaceModule ];
      };
    in
    {
      homeConfigurations = builtins.listToAttrs (map
        (system: { name = "agent-${system}"; value = mkAgent system; })
        systems);
    };
}
