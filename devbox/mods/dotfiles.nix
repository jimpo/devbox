# devbox mod: dotfiles - Jim's shared configuration files in the agent's
# generation, from github.com/jimpo/configenv's own home-manager module, so the
# box stays DRY with the host environment.
#
# The module places ~/.vimrc, ~/.ideavimrc, ~/.tmux.conf, ~/.gitconfig,
# ~/.config/jj/config.toml and ~/.config/nvim as store symlinks, builds vim
# with its plugins through `programs.vim`, writes ~/.zshrc through
# `programs.zsh.oh-my-zsh` (the `jimpo` theme and its custom directory come
# from the store too), and sets EDITOR as a `mkDefault`. It also carries the
# handful of packages those configs expect - fzf, ripgrep, delta, tmux, direnv
# and the rest - which is why nothing here lists them.
#
# CONFIGENV IS A MODULE AND NOTHING MORE: it sets no `home.username`,
# `home.homeDirectory` or `home.stateVersion`, and no `nixpkgs.config`. Those
# belong to whoever imports it, and here that is options.nix, which is the
# devbox equivalent of the `home.nix` configenv's README shows. The README's
# `programs.home-manager.enable` is for a standalone install that runs the
# CLI out of its own profile; `mods/home-manager.sh` runs a pinned one with
# `nix run` instead, so the generation does not carry it.
#
# CONFIGENV CARRIES ITS OWN ENABLE OPTION, and this module sets it rather than
# wrapping the import in `lib.mkIf`: `imports` cannot sit inside a conditional,
# so the file is always imported and `configenv.enable` is what decides whether
# it places anything. `config.devbox.mods.dotfiles.enable` is the flag every
# other module reads, and it means the same here.
#
# `configenv` (the argument) is the input flake.nix hands over through
# `extraSpecialArgs` - the repository itself, taken as a non-flake input.
# `configenv` (the attribute set below) is the option namespace its module
# declares. Same name, two different things.
#
# `.outPath` is not decoration: an `imports` entry that is an attribute set is
# read as an inline module, and a non-flake input IS an attribute set (outPath,
# narHash, lastModified, ...). Naming the store path imports the directory,
# which resolves to configenv's default.nix.
#
# AGENT-ONLY, like the whole generation: the box's privileged account keeps
# stock apt bash and unconfigured vim, deliberately independent of any
# per-account setup that could itself need fixing (see flake.nix's
# "home-manager is agent-only" note). Nothing of this is apt any more: the
# packages the configs expect come from configenv's own module, in the agent's
# profile, and the box's `box-packages` list holds only what has to be the
# machine's.
#
# `configenv.desktop.enable` stays at its default of false: emacs, fonts, the
# VSCode keybindings, the base16-shell palette and the rsync backup filter are
# a workstation's, and a box is headless. oh-my-zsh's base16 hook is still in
# the custom directory the module links, and it sources nothing when
# ~/.config/base16-shell is absent, so the box's zsh keeps its terminal's own
# colors.
{ config, configenv, ... }:
{
  imports = [ configenv.outPath ];

  configenv.enable = config.devbox.mods.dotfiles.enable;
}
