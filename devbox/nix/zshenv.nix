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
# home-manager's own block comes FIRST in the file it writes, and `envExtra` is
# appended after it, which is what makes the PATH order below work out.
#
# NIX IS HERE BECAUSE ITS OWN WIRING IS NOT ENOUGH, exactly as in ~/.bash_env.
# The installer edits /etc/profile.d, /etc/bash.bashrc and /etc/zsh/zshrc - a
# login shell's and an interactive shell's files. A non-interactive zsh reads
# /etc/zsh/zshenv and this file and nothing else, so without the source below
# it has ~/.local/bin (from home.sessionPath, through hm-session-vars.sh) and
# no generation at all: every tool that moved into the store becomes invisible
# to `ssh <agent-alias> '<cmd>'`, while the binaries a bash mod left in
# ~/.local/bin keep answering. The profile script prepends ~/.nix-profile/bin,
# so sourcing it after home-manager's block puts the generation AHEAD of
# ~/.local/bin - the declarative answer first, and a leftover imperative
# binary unable to shadow it.
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

    # The box-wide multi-user store: NIX_PROFILES, NIX_SSL_CERT_FILE and both
    # profiles' bin, this account's first. The script's own guard makes a
    # second sourcing a no-op, and it handles zsh's word splitting itself.
    # Both spellings, because which one the installer writes has changed
    # across nix releases.
    if [ -r /etc/profile.d/nix.sh ]; then
      . /etc/profile.d/nix.sh
    elif [ -r /etc/profile.d/nix-daemon.sh ]; then
      . /etc/profile.d/nix-daemon.sh
    fi
  '';
}
