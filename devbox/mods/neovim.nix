# devbox mod: neovim - the editor in the agent's own profile, built from the
# flake's nixpkgs pin (neovim-unwrapped 0.12.4), together with the tree-sitter
# CLI its config compiles parsers with.
#
# THE ADMIN HAS NO nvim. Home-manager is agent-only, so `nvim` now exists in an
# attachment's profile and nowhere else; the box's privileged account keeps the
# apt `vim` the init script installs, which is what `devbox box ssh` uses.
#
# `dotfiles` OWNS ~/.config/nvim. That module links configenv's whole `nvim`
# directory there, so `sideloadInitLua` keeps this one off the same path: the
# generated lua (the provider stubs, the lua package path) is loaded through a
# `--cmd 'lua dofile(...)'` wrapper argument before the user's init.lua
# instead of being written to ~/.config/nvim/init.lua. Without it the two
# would be writing into each other's directory.
#
# NO `programs.neovim.plugins`, for the same reason. configenv's init.lua
# declares its plugins with `vim.pack.add`, which installs them under
# ~/.local/share/nvim/site/pack/core; a plugin listed here lands in
# .../site/pack/hm, and nvim would load both copies of it. The plugin set is
# the config's to choose, and this module supplies what that config needs to
# run: the editor, and the CLI below.
#
# `pkgs.tree-sitter` IS STILL NEEDED. The config calls
# `require('nvim-treesitter').install {...}`, which fetches and compiles each
# grammar with the `tree-sitter` CLI at nvim run time. Prebuilt grammars from
# `vimPlugins.nvim-treesitter.withAllGrammars` would not be reached by that
# call, because they belong to a different copy of the plugin (see above), so
# the CLI goes in the profile the way the tarball used to go in /usr/local.
{ config, lib, pkgs, ... }:
lib.mkIf config.devbox.mods.neovim.enable {
  programs.neovim = {
    enable = true;
    sideloadInitLua = true;
  };

  home.packages = [ pkgs.tree-sitter ];
}
