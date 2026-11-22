# Music2DAP

Spotify links in, tagged audio on your DAP out. No Spotify API key: metadata and
cover come from Spotify's public pages, audio is matched on YouTube Music and
transcoded by ffmpeg. Lyrics come from LRCLIB, then Genius, then YouTube Music.

```nix
{
  inputs.music2dap.url = "github:MirzaAyBaig12/music2dap";
  inputs.music2dap.inputs.nixpkgs.follows = "nixpkgs";
}
```

Apply `overlays.default` (or import `homeManagerModules.music2dap` /
`nixosModules.music2dap`) and then:

```nix
programs.music2dap = {
  enable = true;
  musicDeviceMount = "HiBy R1 SD";
  preferredFormat = "FLAC";
};
```

That puts `music2dap` in PATH. Paste Spotify track/album/playlist links, one per
line, then type `done`. Files land in:

```
/run/media/$USER/<device>/Music/<Artist>/<Album>/<NN - Title>.flac
```

Albums with a single track (and tracks with no album) go into
`<Artist>/Singles/`.

## Formats

`preferredFormat` accepts `FLAC` (default), `MP3` and `OPUS`. Override per run
with `music2dap --format MP3` or `$MUSIC2DAP_FORMAT`.

## Refreshing and checking an existing library

```sh
# A bare root is scanned at <root>/Music when that folder exists.
music2dap --update-metadata ~/Music/HiBy
music2dap --doctor ~/Music/HiBy
```

`--update-metadata` reads the Spotify URL from the `SOURCE` tag (falls back to
`COMMENT`, then a Spotify search), refreshes tags from Spotify, fills gaps from
YT Music only, embeds lyrics and writes files in place — nothing is moved or
renamed. `--doctor` groups tracks by artist and album and reports which ones
are missing a source link, cover, album or artist. Both print a summary; a
track with no lyrics is reported explicitly under "without lyrics".

## GUI

The Flutter app lives in `gui/` and builds for Windows and Linux.

On Windows, download `Music2DAP-setup.exe` from the `Build Windows GUI` Actions
artifact `music2dap-windows-installer`. The installer puts the app in Program
Files and bundles `yt-dlp.exe`, `ffmpeg.exe` and `ffprobe.exe` in its `tools\`
folder, so no separate dependency setup is needed. Use **Settings → Check
tools** to verify them. A portable zip (`music2dap-windows`) is also published
by the same workflow.

On Linux/NixOS the Nix package already supplies the tools. For other distros run
`installer/install-deps.sh`, then set the paths in **Settings** if they are not
on `PATH`.

Settings (gear icon in the title bar, or the Settings tab) controls:

- **Player / music folder (full path)** — where downloads go; leave blank to
  compose it from mount root + device name + music folder.
- Output format (`FLAC`, `MP3`, `OPUS`).
- `yt-dlp` and `ffmpeg` executables (`ffprobe` is derived from `ffmpeg`).
  **Use bundled** picks up the tools shipped by the installer.

## License

MIT
