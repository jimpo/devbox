# devbox: the base profile - what a workspace with no `default.nix` of its own
# gets. A workspace's `default.nix` usually starts by importing this
# (`devboxModules.default`) and adds to it; leaving it out opts out of all of it.
#
# jj and github-auth are not here: the flake imports them into every
# generation, since `repo-bootstrap` clones with them.
{ ... }:
{
  imports = [ ./dotfiles.nix ./neovim.nix ./cargo-binstall.nix ./claude-code.nix ];
}
