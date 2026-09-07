# devbox mod: dotfiles - Jim's shared configuration files in the agent's
# generation, from github.com/jimpo/configenv's own home-manager module, so the
# box stays DRY with the host environment.
#
# The module places ~/.vimrc, ~/.ideavimrc, ~/.tmux.conf, ~/.gitconfig,
# ~/.config/jj/config.toml, ~/.config/nvim and ~/.config/base16-shell as store
# symlinks, and writes ~/.zshrc through `programs.zsh.oh-my-zsh` (the `jimpo`
# theme and its custom directory come from the store too). It also carries the
# handful of packages those configs expect - fzf, ripgrep, delta, tmux and the
# rest - which is why nothing here lists them.
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
# "home-manager is agent-only" note). The apt package set the configs assume
# system-wide is the `dotfiles-packages` mod, which runs once per box, as root.
#
# `configenv.desktop.enable` stays at its default of false: emacs, fonts and
# the VSCode keybindings are a workstation's, and a box is headless.
{ config, configenv, ... }:
{
  imports = [ configenv.outPath ];

  configenv.enable = config.devbox.mods.dotfiles.enable;
}
