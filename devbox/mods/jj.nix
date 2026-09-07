# devbox: jj (jujutsu) and this workspace's author identity.
#
# The version is nixpkgs': `pkgs.jujutsu` at the rev flake.nix pins, so bumping
# the pin bumps jj.
#
# `dotfiles` OWNS ~/.config/jj/config.toml. That module places configenv's own
# config.toml there, so this one stays off the path entirely - the same split
# `neovim.nix` makes for ~/.config/nvim. Two modules defining one `home.file`
# entry is an evaluation conflict, and either of them claiming a path some
# script writes on the box is a switch that aborts on its own stale
# `<file>.devbox-backup` the second time round.
#
# `programs.jujutsu` is deliberately unused. It claims config.toml as soon as
# its `settings` is non-empty, and `settings` is not only ours to keep empty -
# the module's `ediff` option defaults to `programs.emacs.enable` and would
# fill it in on its own the day some other module enables emacs.
# `home.packages` puts the same package in the profile and can never take the
# file.
#
# THE IDENTITY GOES IN conf.d. jj reads `~/.config/jj/conf.d/*.toml` in
# lexicographic order AFTER config.toml, so these values win over configenv's
# with neither half touching the other's file. The `00-` prefix sorts this file
# first, which leaves any other name in that directory able to override it in
# turn - the by-hand route on the box:
#
#     printf '[ui]\npager = "cat"\n' > ~/.config/jj/conf.d/local.toml
#
# Per-repo settings are unaffected: `jj config set --repo` writes inside the
# checkout's own .jj directory.
#
# The identity is the workspace's, not the box's: every checkout in this
# account shares it, which is the point of putting several repos in one
# workspace.
{ config, lib, pkgs, ... }:
lib.mkIf config.devbox.mods.jj.enable {
  home.packages = [ pkgs.jujutsu ];

  home.file.".config/jj/conf.d/00-devbox.toml".source =
    (pkgs.formats.toml { }).generate "devbox-jj-identity.toml" {
      user = {
        name = config.devbox.workspace.jjName;
        email = config.devbox.workspace.jjEmail;
      };
    };
}
