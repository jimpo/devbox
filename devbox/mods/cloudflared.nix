# devbox: cloudflared - this account's Cloudflare tunnel, as a systemd user
# service.
#
# ONE TUNNEL PER ATTACHMENT. The service lives in the agent's own user manager
# and reads the agent's own token, so several workspaces on one box each hold
# their own tunnel without knowing about each other.
#
# The driver enables lingering for the account (`loginctl enable-linger`, in
# ensure_agent), so this service starts at boot and keeps running between
# logins rather than only for the length of the provisioning SSH session.
#
# cloudflared comes from `pkgs-unstable`, the flake's commit-pinned
# nixpkgs-unstable, because 26.05's is missing a security fix. When 26.05
# carries the fixed version, go back to `pkgs.cloudflared` (see flake.nix).
#
# THE TOKEN NEVER ENTERS THE STORE. /nix/store is world-readable and shared by
# every attachment on the box, so the only thing below that mentions the token
# is the PATH of the 0600 file it lives in. The two wrapper scripts read that
# file at service start, as the agent, and hand the value to cloudflared in the
# environment - `TUNNEL_TOKEN`, which cloudflared reads on its own - so it is
# absent from the command line and therefore from `ps` for every other account.
{ config, lib, pkgs, pkgs-unstable, ... }:

let
  # The credentials file `devbox sync --envvars` writes into this account's
  # home. It is SOURCED rather than named in `EnvironmentFile=` because it is
  # shell syntax: devbox writes `export KEY=<shell-quoted value>` per entry,
  # and systemd's environment-file parser accepts bare `KEY=value` only - it
  # reads everything up to the `=` as the name, finds `export KEY` invalid, and
  # drops the line without failing. Sourcing also lets the workspace keep
  # naming the value `CLOUDFLARE_TOKEN` while cloudflared reads `TUNNEL_TOKEN`.
  envFile = "${config.devbox.workspace.homeDirectory}/.config/devbox/env";
  quotedEnvFile = lib.escapeShellArg envFile;

  # `ExecCondition=` is systemd's "should this unit run at all": exiting 1-254
  # skips the service and leaves the unit inactive, NOT failed. That is what
  # answers the workspace which enabled the mod but configured no token - the
  # tunnel simply does not run - and it is also why the file not existing yet
  # is harmless. `devbox sync --envvars` and the provisioning run have no fixed
  # order between them, and the switch happens whether or not the file has
  # landed.
  configured = pkgs.writeShellScript "devbox-cloudflared-configured" ''
    [ -f ${quotedEnvFile} ] || exit 1
    . ${quotedEnvFile}
    [ -n "''${CLOUDFLARE_TOKEN:-}" ]
  '';

  # `--no-autoupdate` is a flag of `tunnel`, hence its place before the
  # subcommand. cloudflared lives in the read-only store, and an update would
  # restart the process underneath systemd.
  run = pkgs.writeShellScript "devbox-cloudflared-run" ''
    set -eu
    . ${quotedEnvFile}
    export TUNNEL_TOKEN="$CLOUDFLARE_TOKEN"
    exec ${lib.getExe pkgs-unstable.cloudflared} tunnel --no-autoupdate run
  '';
in
lib.mkIf config.devbox.mods.cloudflared.enable {
  home.packages = [ pkgs-unstable.cloudflared ];

  systemd.user.services.cloudflared = {
    Unit = {
      Description =
        "Cloudflare tunnel for ${config.devbox.workspace.agentUser}";
      # The restart counter's window is wide enough to hold a full burst of
      # `RestartSec` retries, so a token the tunnel cannot authenticate with
      # runs out of restarts and settles in `failed`, where `devbox status`
      # reports it. Without a window that outlasts the burst, such a service
      # would restart every ten seconds forever and never reach a state anyone
      # can see. A single restart every minute or so - a network blip, which
      # cloudflared mostly rides out internally anyway - stays under the limit
      # and keeps the tunnel up.
      StartLimitIntervalSec = 300;
      StartLimitBurst = 5;
    };

    Service = {
      # cloudflared runs in the foreground and logs to stdout/stderr, which
      # systemd puts in the journal: `journalctl --user -u cloudflared`.
      Type = "simple";
      ExecCondition = "${configured}";
      ExecStart = "${run}";
      Restart = "always";
      RestartSec = 10;
    };

    Install.WantedBy = [ "default.target" ];
  };
}
