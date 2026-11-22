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

## License

MIT
