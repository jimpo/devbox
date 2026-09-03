# devbox: vim for the agent account - the plugin set from the store, and the
# account's own ~/.vimrc for everything else.
#
# THE PLUGINS ARE DECLARED HERE, not read from ~/.vimrc. That file is written
# by the `dotfiles` mod from configenv, on the box, long after this flake is
# evaluated; nix cannot learn a plugin list from it. The three below are the
# ones configenv's vimrc asks for, so the declaration is the same set stated in
# the half that can fetch it. Settings, mappings and the colorscheme stay in
# that file - this module owns only what gets installed.
#
# ~/.vimrc IS STILL READ. `vim.customize` wraps the binary as
# `vim -u <generated vimrc>`, and `-u` makes vim skip the user's own file, so
# the generated one sources it back. `packloadall` goes first because these
# plugins arrive through vim's package mechanism, which vim applies AFTER the
# vimrc, while configenv's file runs `colorscheme base16-chalk` during it.
#
# CONFIGENV'S PLUGIN MANAGER STILL RUNS, harmlessly. Its vimrc calls
# `plug#begin()` and names the same three plugins, and `dotfiles` installs the
# plug.vim that answers those calls, so nothing errors. Nothing fetches the
# plugins into ~/.vim/plugged for an agent, so vim-plug contributes empty
# directories to 'runtimepath' and the store copies are what load.
#
# The box's privileged account is not covered by any of this: it has no
# generation, no dotfiles and no vim-plug of its own any more - it keeps
# whatever apt's `vim` ships with, unconfigured.
{ config, lib, pkgs, ... }:
lib.mkIf config.devbox.mods.vim.enable {
  programs.vim = {
    enable = true;

    # A terminal vim. The option's default is `vim-full`, the GUI build, whose
    # closure a headless box has no use for.
    packageConfigurable = pkgs.vim;

    # home-manager appends vim-sensible to whatever this names; it loads before
    # ~/.vimrc is sourced, so configenv's own settings still win.
    plugins = with pkgs.vimPlugins; [ base16-vim rust-vim vim-go ];

    extraConfig = ''
      packloadall
      if filereadable(expand('~/.vimrc'))
        source ~/.vimrc
      endif
    '';
  };
}
