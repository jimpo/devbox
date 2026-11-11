# devbox: uv - the Python package and tool manager, for a workspace's own
# Python projects and for installing Python CLIs as uv tools.
{ config, lib, pkgs, ... }:

{
  home.packages = [ pkgs.uv ];
}
