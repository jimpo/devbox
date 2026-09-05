# devbox: nodejs - the Node.js LTS runtime, with npm, npx and corepack.
#
# `pkgs.nodejs` is nixpkgs' LTS ALIAS, not its newest node - `pkgs.nodejs_latest`
# is that, and is deliberately not what this installs. So the LTS major moves
# when flake.nix's nixpkgs pin does, and never between two switches of the same
# pin. npm ships inside that package (`nodejs-slim` is the build without it),
# so there is no second package to install for it.
#
# NPM'S GLOBAL PREFIX MOVES INTO THE ACCOUNT. `npm install -g` writes under the
# prefix, which defaults to the node package's own store path - read-only, so an
# unconfigured global install fails outright. NPM_CONFIG_PREFIX points it at a
# directory the account owns and home.sessionPath puts that directory's bin on
# PATH. Both land in hm-session-vars.sh, which the agent's ~/.zshenv and the
# driver's ~/.bash_env source, so `ssh <box> '<cmd>'` finds a globally installed
# CLI too. The `$HOME` is expanded by the shell that sources that file, which is
# the same trick `cargo-binstall.nix` uses for ~/.cargo/bin.
#
# An env var rather than `npm config set prefix`, which writes ~/.npmrc: that
# leaves the account holding a file the generation cannot reconverge, on a path
# an agent has its own reasons to edit (registries, auth tokens).
{ config, lib, pkgs, ... }:

lib.mkIf config.devbox.mods.nodejs.enable {
  home.packages = [ pkgs.nodejs ];

  home.sessionVariables.NPM_CONFIG_PREFIX = "$HOME/.npm-global";
  home.sessionPath = [ "$HOME/.npm-global/bin" ];
}
