# devbox: the agent account's ~/.zshenv - the one file EVERY zsh reads, and the
# zsh counterpart of the ~/.bash_env the provisioning driver writes for bash. A
# human at a shell reads it, and so does `ssh <box> '<cmd>'`, which runs the
# login shell non-interactively and never reaches ~/.zshrc.
#
# ALWAYS PART OF THE GENERATION, and gated on no `devbox.mods.<name>.enable`
# flag: this is devbox's own shell plumbing, not an optional mod. That is why it
# sits beside options.nix rather than in modules/ - a file there is a mod NAME,
# one devbox resolves against the playbook and renders a flag for.
#
# `programs.zsh` IS THE ONE WRITER OF ~/.zshenv, which is why this file enables
# it and appends through `envExtra` rather than claiming the path with a
# `home.file`. home-manager writes that file from the zsh module itself - the
# generation's session variables and PATH go in it - and a `home.file` entry
# for the same path is a second thing writing it, which the `home-manager-files`
# build refuses. `dotfiles`/configenv enables `programs.zsh` too, for oh-my-zsh
# and ~/.zshrc; enabling it here as well is what keeps ~/.zshenv in the
# generation for an account whose playbook has no `dotfiles`.
#
# NO CREDS, as everywhere in this flake: /nix/store is world-readable and shared
# by every attachment on the box. The file SOURCES the account's 0600
# ~/.config/devbox/env (written by `devbox sync --envvars`); no value from it is
# ever in a nix expression. The `[ -f ]` guard is what keeps the two independent
# - the env file is written by a different code path, and a zsh that starts
# before it exists simply has no creds.
{ ... }:
{
  # Where the bash mods install their binaries (claude, codex), and where the
  # codex installer looks before deciding whether to edit a shell profile.
  # home.sessionPath lands in hm-session-vars.sh, which ~/.zshenv sources - as
  # does any module's home.sessionVariables.
  home.sessionPath = [ "$HOME/.local/bin" ];

  programs.zsh.enable = true;

  programs.zsh.envExtra = ''
    # This account's devbox-managed [env] creds, written 0600 by
    # `devbox sync --envvars`.
    [ -f "$HOME/.config/devbox/env" ] && . "$HOME/.config/devbox/env"
  '';
}
