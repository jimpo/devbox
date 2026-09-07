# devbox mod: vim for the agent account - the plugin set from the store, and
# the account's own ~/.vimrc for everything else.
#
# THE PLUGINS ARE DECLARED HERE, not read from ~/.vimrc. The three below are
# the ones configenv's vimrc asks for, stated in the half that can fetch them;
# `vim-sensible` is the baseline over which that file's own settings win.
# Settings, mappings and the colorscheme stay in ~/.vimrc - this module owns
# only what gets installed.
#
# ~/.vimrc IS STILL READ. `customize` wraps the binary as `vim -u <generated
# vimrc>`, and `-u` makes vim skip the user's own file, so the generated one
# sources it back. `packloadall` goes first because these plugins arrive
# through vim's package mechanism, which vim applies AFTER the vimrc, while
# configenv's file runs `colorscheme base16-chalk` during it.
#
# `lib.hiPrio` IS LOAD-BEARING, and is why this module builds the package by
# hand rather than through `programs.vim`. configenv's module puts a plain
# `pkgs.vim` in the same profile, two derivations cannot both own
# ~/.nix-profile/bin/vim, and `home.packages` is a merged list no module can
# take an entry out of. A priority makes the choice explicit instead:
# `buildEnv` gives every path priority 5 unless its meta says otherwise, and
# the wrapper below asks for -10, so it is the `vim` on PATH and the plain one
# is skipped without a collision. `programs.vim` would add its package at the
# default priority, which collides.
#
# The box's privileged account is not covered by any of this: it has no
# generation and no dotfiles, so it keeps whatever apt's `vim` ships with,
# unconfigured.
{ config, lib, pkgs, ... }:
lib.mkIf config.devbox.mods.vim.enable {
  home.packages = [
    (lib.hiPrio (pkgs.vim.customize {
      # The wrapper's binaries: vim, vimdiff, rvim and the rest.
      name = "vim";

      vimrcConfig = {
        customRC = ''
          packloadall
          if filereadable(expand('~/.vimrc'))
            source ~/.vimrc
          endif
        '';

        # vim's own package mechanism, under a devbox-named package directory.
        packages.devbox.start = with pkgs.vimPlugins; [
          vim-sensible
          base16-vim
          rust-vim
          vim-go
        ];
      };
    }))
  ];
}
