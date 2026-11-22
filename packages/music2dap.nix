{
  lib,
  stdenvNoCC,
  python3,
  ffmpeg,
  nodejs,
  makeWrapper,
  # Name of the mounted device, i.e. the folder /run/media/$USER/<musicDeviceMount>.
  # programs.music2dap sets this from its `musicDeviceMount` option.
  musicDeviceMount ? "HiBy R1 SD",
  mountRoot ? "/run/media/$USER",
  musicDir ? "Music",
  # Output format: FLAC, MP3 or OPUS.
  preferredFormat ? "FLAC",
}:

# Music2DAP: paste Spotify links, get tagged audio in
# <device>/Music/<Artist>/<Album>/ (Singles for one-track albums).
#   - Spotify metadata + cover: public pages, no API key
#   - audio: matched on YouTube Music, downloaded with yt-dlp, transcoded by ffmpeg
#   - YT Music only fills fields Spotify doesn't know
#   - lyrics: LRCLIB, Genius, then YT Music
#   - SOURCE tag stores the Spotify URL for --update-metadata / --doctor
let
  pythonEnv = python3.withPackages (ps: [
    ps.mutagen
    ps.requests
    ps.rich
    ps.yt-dlp
    ps.ytmusicapi
  ]);
in
stdenvNoCC.mkDerivation {
  pname = "music2dap";
  version = "1.0.0";

  src = ../src;

  dontBuild = true;

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall

    install -Dm644 music2dap.py $out/share/music2dap/music2dap.py

    # nodejs is yt-dlp's JS runtime for YouTube's challenges.
    makeWrapper ${pythonEnv}/bin/python3 $out/bin/music2dap \
      --add-flags $out/share/music2dap/music2dap.py \
      --prefix PATH : ${lib.makeBinPath [ ffmpeg nodejs ]} \
      --set-default MUSIC2DAP_DEVICE ${lib.escapeShellArg musicDeviceMount} \
      --set-default MUSIC2DAP_MUSIC_DIR ${lib.escapeShellArg musicDir} \
      --set-default MUSIC2DAP_MOUNT_ROOT ${lib.escapeShellArg mountRoot} \
      --set-default MUSIC2DAP_FORMAT ${lib.escapeShellArg preferredFormat}

    runHook postInstall
  '';

  meta = {
    description = "Music2DAP — Spotify links to tagged audio for your DAP (terminal UI)";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "music2dap";
  };
}
