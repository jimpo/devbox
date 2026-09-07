# devbox: github-auth - wire git/jj HTTPS auth for github.com to a
# fine-grained PAT. `gh` reads GITHUB_TOKEN/GH_TOKEN from the environment
# natively (no `gh auth login`, so nothing lands in ~/.config/gh/hosts.yml);
# this points git's credential helper at gh, so `git`/`jj git` HTTPS -
# including repo-bootstrap's private `jj git clone` - authenticate
# transparently. The clone URL stays plain HTTPS: gh is the credential helper
# behind it, not the transport.
#
# LIVES AT ~/.config/git/config, git's XDG global config - not ~/.gitconfig.
# The `dotfiles` module owns ~/.gitconfig (it is one of the files configenv
# places), and two modules cannot own one file. git reads both, so this one
# takes the XDG path and owns it outright.
#
# The helper line holds no secret: it names a helper, and the helper answers
# with whatever GITHUB_TOKEN is in the CALLING account's own
# ~/.config/devbox/env. Unconditional: with no token the helper prints
# nothing and exits 1, git falls through, and public clones are unaffected.
{ config, lib, pkgs, ... }:

lib.mkIf config.devbox.mods.github-auth.enable {
  home.packages = [ pkgs.gh ];

  home.file.".config/git/config".text = ''
    [credential "https://github.com"]
        helper = !${lib.getExe pkgs.gh} auth git-credential
  '';
}
