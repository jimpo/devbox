# devbox: jj (jujutsu) and this workspace's author identity.
#
# The version is nixpkgs': `programs.jujutsu.package` defaults to
# `pkgs.jujutsu` at the rev flake.nix pins, so bumping the pin bumps jj.
#
# WHO OWNS ~/.config/jj/config.toml. This module does: home-manager writes the
# `settings` below to that path as a symlink into the read-only store, so on
# the box `jj config set --user <key> <value>` fails with a permission error.
# jj writes through the symlink rather than replacing it, so a failed attempt
# leaves the generation intact.
#
# The escape hatch on the box is jj's own config directory. jj loads
# `~/.config/jj/conf.d/*.toml` in lexicographic order AFTER config.toml, and
# nothing manages that directory, so a file dropped there overrides anything
# set here:
#
#     mkdir -p ~/.config/jj/conf.d
#     printf '[ui]\npager = "cat"\n' > ~/.config/jj/conf.d/local.toml
#
# Per-repo settings are unaffected: `jj config set --repo` writes inside the
# checkout's own .jj directory.
#
# The identity is the workspace's, not the box's: every checkout in this
# account shares it, which is the point of putting several repos in one
# workspace.
{ config, lib, ... }:
lib.mkIf config.devbox.mods.jj.enable {
  programs.jujutsu = {
    enable = true;
    settings.user = {
      name = config.devbox.workspace.jjName;
      email = config.devbox.workspace.jjEmail;
    };
  };
}
