# programs.music2dap -- NixOS + Home Manager module.
#
#   programs.music2dap = {
#     enable = true;
#     musicDeviceMount = "HiBy R1 SD";
#     preferredFormat = "FLAC";
#   };
#
# The device is looked up at <mountRoot>/<musicDeviceMount> and files are written to
# <mountRoot>/<musicDeviceMount>/<musicDir>/<Artist>/<Album>/. One-track albums and
# tracks without an album go into <Artist>/Singles/.
{ isHomeManager }:
{ config, lib, pkgs, ... }:
let
  cfg = config.programs.music2dap;

  formats = [ "FLAC" "MP3" "OPUS" ];

  upstream = pkgs.music2dap or (pkgs.callPackage ../packages/music2dap.nix { });

  built = upstream.override {
    musicDeviceMount = cfg.musicDeviceMount;
    mountRoot = cfg.mountRoot;
    musicDir = cfg.musicDir;
    preferredFormat = cfg.preferredFormat;
  };
in
{
  options.programs.music2dap = {
    enable = lib.mkEnableOption "Music2DAP (Spotify links to tagged audio on your DAP)";

    musicDeviceMount = lib.mkOption {
      type = lib.types.str;
      default = "HiBy R1 SD";
      example = "HiBy R1 SD";
      description = ''
        Name of the mounted device, exactly as it appears under `mountRoot`.
        Default: `/run/media/$USER/HiBy R1 SD`.
      '';
    };

    mountRoot = lib.mkOption {
      type = lib.types.str;
      default = "/run/media/$USER";
      description = ''
        Parent directory that mounted removable media appears in. The binary
        expands $USER itself at runtime.
      '';
    };

    musicDir = lib.mkOption {
      type = lib.types.str;
      default = "Music";
      description = "Folder on the device that the per-artist folders go into.";
    };

    preferredFormat = lib.mkOption {
      type = lib.types.enum formats;
      default = "FLAC";
      example = "MP3";
      description = ''
        Output format for downloaded audio. One of
        ${lib.concatStringsSep ", " (map (f: "`${f}`") formats)}.
        Override per run with `music2dap --format`.
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = built;
      defaultText = lib.literalExpression "pkgs.music2dap.override { ... }";
      description = "The Music2DAP package installed into PATH when enabled.";
    };
  };

  config = lib.mkIf cfg.enable (
    if isHomeManager
    then { home.packages = [ cfg.package ]; }
    else { environment.systemPackages = [ cfg.package ]; }
  );
}
