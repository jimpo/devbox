# devbox: uv - the Python package and tool manager, for a workspace's own
# Python projects and for installing Python CLIs as uv tools.
{ config, lib, pkgs, ... }:

lib.mkIf config.devbox.mods.uv.enable {
  home.packages = [ pkgs.uv ];
}
