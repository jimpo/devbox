# devbox: cargo-binstall - installs a Rust CLI tool from a prebuilt binary
# release, for the crates nixpkgs does not carry. It needs no rust toolchain of
# its own: it resolves and fetches crates unaided. Invoke it as
# `cargo-binstall`; the `cargo binstall` subcommand form needs cargo, which a
# box has only if a workspace's own mod installed one.
{ config, lib, pkgs, ... }:

lib.mkIf config.devbox.mods.cargo-binstall.enable {
  home.packages = [ pkgs.cargo-binstall ];

  # Where cargo-binstall installs what it fetches ($CARGO_HOME/bin), which
  # nothing else puts on PATH. home-manager writes this list into
  # hm-session-vars.sh in a double-quoted context, so `$HOME` is expanded by
  # the shell that sources it - the driver's ~/.bash_env does, so a
  # non-interactive `ssh <box> '<cmd>'` finds these binaries too.
  home.sessionPath = [ "$HOME/.cargo/bin" ];
}
