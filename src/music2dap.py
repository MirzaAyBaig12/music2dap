#!/usr/bin/env python3
"""Music2DAP: paste Spotify links, get tagged audio on your DAP.

Metadata and cover art come from Spotify's public pages (no API key needed).
Spotify audio is DRM'd, so the actual audio is matched on YouTube Music and
converted to your preferred format (FLAC by default). YouTube Music is also
the fallback for anything Spotify doesn't know (album, track number, year,
cover), and only for those fields. Lyrics are embedded from LRCLIB, then
Genius, then YouTube Music.

The Spotify URL is stored in the SOURCE tag (and COMMENT for older readers),
so `--update-metadata` can refresh tags and lyrics without needing the link.

Result on the device:
  /run/media/$USER/<device>/Music/<Artist>/<Album>/<NN - Title>.<ext>
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlencode

try:
    import readline  # noqa: F401  (line editing for the URL prompt)
except ImportError:
    pass

import mutagen
import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, USLT
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

__version__ = "1.0.0"

DEFAULT_MOUNT_ROOT = "/run/media/$USER"

# --------------------------------------------------------------------------- #
# Output formats. preferredFormat (Nix module) / --format / $MUSIC2DAP_FORMAT
# picks one; everything downstream uses its extension and ffmpeg settings.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AudioFormat:
    name: str
    ext: str
    codec: str
    codec_args: tuple = ()
    container: str = ""


FORMATS = {
    "FLAC": AudioFormat("FLAC", ".flac", "flac", ("-compression_level", "5")),
    "MP3": AudioFormat("MP3", ".mp3", "libmp3lame", ("-q:a", "2")),
    "OPUS": AudioFormat("OPUS", ".opus", "libopus", ("-b:a", "128k")),
}
ACTIVE_FORMAT = FORMATS["FLAC"]
AUDIO_EXTS = tuple({f.ext for f in FORMATS.values()})


def build_steps(fmt: AudioFormat):
    return [
        ("meta", "Spotify metadata"),
        ("match", "YT Music match"),
        ("dl", "Download audio"),
        ("encode", f"Convert to {fmt.name}"),
        ("tag", "Tag + cover art"),
        ("lyrics", "Lyrics"),
        ("copy", "Copy to device"),
    ]


STEPS = build_steps(ACTIVE_FORMAT)

# Catppuccin Mocha, to match the rest of Axiom.
MAUVE, TEXT, SUB, DIM = "#cba6f7", "#cdd6f4", "#a6adc8", "#6c7086"
SURFACE, BASE = "#313244", "#1e1e2e"
GREEN, RED, YELLOW = "#a6e3a1", "#f38ba8", "#f9e2af"

console = Console(highlight=False)


class PipelineError(Exception):
    pass


class SpotifyError(PipelineError):
    pass


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


_BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')


def safe_name(name: str, fallback: str = "Unknown", maxlen: int = 110) -> str:
    """Make a string safe for FAT32/exFAT file and folder names."""
    name = name.replace("/", "-").replace("\\", "-").replace(":", " -")
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:maxlen].strip(" .")
    return name or fallback


_BRACKETS = re.compile(r"[\(\[].*?[\)\]]")


def artist_label(artists: list[str]) -> str:
    """Maher Zain + Mesut Kurtis -> 'Maher Zain feat. Mesut Kurtis'."""
    if not artists:
        return ""
    if len(artists) == 1:
        return artists[0]
    return f"{artists[0]} feat. " + " & ".join(artists[1:])


def norm(s: str) -> str:
    """Normalise a title/artist for fuzzy comparison (keeps non-Latin scripts)."""
    base = unicodedata.normalize("NFKC", s or "").casefold()
    # NFKC expands ﷺ into this phrase; drop it so "Song ﷺ" == "Song".
    base = base.replace("صلى الله عليه وسلم", " ").replace("\u0640", "")
    stripped = _BRACKETS.sub(" ", base)
    stripped = re.sub(r"\s-\s.*$", " ", stripped)
    out = re.sub(r"[\W_]+", " ", stripped).strip()
    return out or re.sub(r"[\W_]+", " ", base).strip()


# --------------------------------------------------------------------------- #
# the device
# --------------------------------------------------------------------------- #

def resolve_device_path(root: Path, device: str, music_dir: str) -> Path:
    """<root>/<device>, unless <root> is already the device (custom --mount-root)."""
    nested = root / device
    if nested.is_dir() or not (root / music_dir).is_dir():
        return nested
    return root


@dataclass
class Device:
    name: str
    root: Path
    music_dir: str
    device_path: Path | None = None

    def __post_init__(self):
        self.root = Path(self.root).expanduser()
        self.device_path = resolve_device_path(self.root, self.name, self.music_dir)

    @property
    def path(self) -> Path:
        return self.device_path or (self.root / self.name)

    @property
    def library(self) -> Path:
        return self.path / self.music_dir

    def present(self) -> bool:
        return self.path.is_dir() and os.access(self.path, os.W_OK | os.X_OK)

    def usage(self):
        try:
            return shutil.disk_usage(self.path)
        except OSError:
            return None


def banner(dev: Device) -> Panel:
    ok = dev.present()
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=DIM, justify="right")
    grid.add_column()
    grid.add_row(
        "device",
        Text.assemble(("● " if ok else "○ ", GREEN if ok else RED), (dev.name, f"bold {TEXT}")),
    )
    grid.add_row("folder", Text(str(dev.library), style=SUB))
    usage = dev.usage() if ok else None
    if usage:
        width = 22
        filled = round(usage.used / usage.total * width) if usage.total else 0
        bar = Text("▰" * filled, style=MAUVE) + Text("▱" * (width - filled), style=DIM)
        grid.add_row(
            "space",
            Text.assemble(bar, (f"  {fmt_bytes(usage.free)} free of {fmt_bytes(usage.total)}", SUB)),
        )
    title = Text.assemble(("♫ ", MAUVE), ("Music2DAP", f"bold {MAUVE}"), ("  v" + __version__, DIM))
    return Panel(
        Group(Text(f"Spotify → {ACTIVE_FORMAT.name} → your player", style=SUB), Text(""), grid),
        title=title,
        title_align="left",
        border_style=MAUVE,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def device_not_mounted(mount_root: str, device: str, expected: str) -> None:
    """--doctor / --update-metadata need the DAP mounted; say so clearly."""
    root = Path(mount_root).expanduser()
    try:
        mounted = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        mounted = []
    body = Text()
    body.append("No device mounted — please plug in your DAP.\n", style=f"bold {RED}")
    body.append(f"looked for {expected}\n", style=SUB)
    if mounted:
        body.append("mounted right now: ", style=DIM)
        body.append(", ".join(mounted), style=TEXT)
    else:
        body.append("nothing is mounted under ", style=DIM)
        body.append(str(root), style=TEXT)
    console.print(Panel(body, border_style=RED, box=box.ROUNDED, padding=(1, 2)))
    raise SystemExit(1)


def wait_for_device(dev: Device) -> None:
    while not dev.present():
        try:
            mounted = sorted(p.name for p in dev.root.iterdir() if p.is_dir())
        except OSError:
            mounted = []
        body = Text()
        body.append(f"Can't find “{dev.name}”\n", style=f"bold {RED}")
        body.append(f"looked in {dev.path}\n\n", style=SUB)
        if mounted:
            body.append("mounted right now: ", style=DIM)
            body.append(", ".join(mounted), style=TEXT)
        else:
            body.append("nothing is mounted under ", style=DIM)
            body.append(str(dev.root), style=TEXT)
        console.print(Panel(body, border_style=RED, box=box.ROUNDED, padding=(1, 2)))
        if not sys.stdin.isatty():
            raise SystemExit(1)
        try:
            ans = console.input(Text("  mount it, then press enter to retry (q to quit) ", style=SUB))
        except (EOFError, KeyboardInterrupt):
            console.print()
            raise SystemExit(1)
        if ans.strip().lower() in ("q", "quit", "exit"):
            raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Spotify (public pages only)
# --------------------------------------------------------------------------- #

# Deliberately NOT a full browser UA: Spotify serves a bare SPA shell to those, and
# the server-rendered <meta> tags (album, track number, cover) to plain clients.
UA = "Mozilla/5.0"
http = requests.Session()
http.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

# Genius refuses the bare UA that Spotify serves its metadata to, so lyrics get
# their own client with a normal browser UA. No API keys anywhere.
lyric_http = requests.Session()
lyric_http.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

SPOTIFY_LINK = re.compile(
    r"open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?(?:embed/)?(track|album|playlist)/([A-Za-z0-9]{22})"
)
SPOTIFY_URI = re.compile(r"spotify:(track|album|playlist):([A-Za-z0-9]{22})")


def parse_spotify(token: str):
    m = SPOTIFY_LINK.search(token) or SPOTIFY_URI.search(token)
    return (m.group(1), m.group(2)) if m else None


def get(url: str) -> requests.Response:
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = http.get(url, timeout=20)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(1 + attempt)
    raise PipelineError(f"network error: {last}")


def get_quiet(url: str, session=None, timeout: int = 15):
    """A GET where 404/blocks are an ordinary 'not found', not a retry storm."""
    try:
        r = (session or http).get(url, timeout=timeout)
        return r if r.ok else None
    except requests.RequestException:
        return None


def meta_tags(page: str) -> list[tuple[str, str]]:
    out = []
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        key = re.search(r'\b(?:name|property)="([^"]+)"', tag)
        val = re.search(r'\bcontent="([^"]*)"', tag)
        if key and val:
            out.append((key.group(1), html.unescape(val.group(1))))
    return out


def embed_entity(kind: str, sid: str) -> dict:
    page = get(f"https://open.spotify.com/embed/{kind}/{sid}").text
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        raise SpotifyError("Spotify embed page had no data (did Spotify change it?)")
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, ValueError) as e:
        raise SpotifyError("couldn't parse Spotify embed data") from e


@dataclass
class Album:
    id: str
    name: str = ""
    artist: str = ""
    date: str = ""
    cover: str = ""
    tracks: list = field(default_factory=list)  # (track_id, disc, number)


_albums: dict[str, Album] = {}


def spotify_album(aid: str) -> Album:
    if aid in _albums:
        return _albums[aid]
    a = Album(aid)
    try:
        ent = embed_entity("album", aid)
        a.name = ent.get("name") or ent.get("title") or ""
        a.artist = ent.get("subtitle") or ""
        for n, item in enumerate(ent.get("trackList") or [], 1):
            m = re.match(r"spotify:track:([A-Za-z0-9]{22})", item.get("uri", ""))
            if m:
                a.tracks.append((m.group(1), 1, n))
    except PipelineError:
        pass
    try:
        tags = meta_tags(get(f"https://open.spotify.com/album/{aid}").text)
        first: dict[str, str] = {}
        for k, v in tags:
            first.setdefault(k, v)
        a.date = first.get("music:release_date", "")
        a.cover = first.get("og:image", "")
        songs: list[list] = []
        for k, v in tags:
            if k == "music:song":
                m = re.search(r"/track/([A-Za-z0-9]{22})", v)
                songs.append([m.group(1) if m else "", 1, len(songs) + 1])
            elif k == "music:song:disc" and songs and v.isdigit():
                songs[-1][1] = int(v)
            elif k == "music:song:track" and songs and v.isdigit():
                songs[-1][2] = int(v)
        if songs:  # the page knows disc numbers, so prefer it over the embed order
            a.tracks = [tuple(s) for s in songs if s[0]]
        if not a.name:
            m = re.match(r"Listen to (.*) - Album by .* \| Spotify", first.get("og:title", ""))
            a.name = m.group(1) if m else ""
    except PipelineError:
        pass
    _albums[aid] = a
    return a


@dataclass
class Track:
    id: str
    title: str = ""
    artists: list = field(default_factory=list)
    album: str = ""
    album_artist: str = ""
    album_id: str = ""
    track_no: int = 0
    track_total: int = 0
    disc_no: int = 0
    disc_total: int = 0
    date: str = ""
    duration_ms: int = 0
    cover_url: str = ""


def spotify_track(sid: str) -> Track:
    t = Track(sid)
    first: dict[str, str] = {}
    for k, v in meta_tags(get(f"https://open.spotify.com/track/{sid}").text):
        first.setdefault(k, v)
    t.title = first.get("og:title", "")
    t.cover_url = first.get("og:image", "")
    t.date = first.get("music:release_date", "")
    if first.get("music:duration", "").isdigit():
        t.duration_ms = int(first["music:duration"]) * 1000
    if first.get("music:album:track", "").isdigit():
        t.track_no = int(first["music:album:track"])
    m = re.search(r"/album/([A-Za-z0-9]{22})", first.get("music:album", ""))
    t.album_id = m.group(1) if m else ""

    try:
        ent = embed_entity("track", sid)
        t.artists = [a["name"] for a in ent.get("artists") or [] if a.get("name")]
        t.title = t.title or ent.get("title") or ent.get("name") or ""
        if not t.duration_ms:
            t.duration_ms = int(ent.get("duration") or 0)
        if not t.date:
            t.date = ((ent.get("releaseDate") or {}).get("isoString") or "")[:10]
    except PipelineError:
        pass
    if not t.artists and first.get("music:musician_description"):
        t.artists = [first["music:musician_description"]]
    if not t.title or not t.artists:
        raise SpotifyError("couldn't read title/artist from Spotify (removed or region-locked?)")

    if t.album_id:
        al = spotify_album(t.album_id)
        t.album, t.album_artist = al.name, al.artist
        for tid, disc, no in al.tracks:
            if tid == sid:
                t.disc_no, t.track_no = disc, no or t.track_no
        if t.disc_no:
            t.track_total = sum(1 for _, d, _ in al.tracks if d == t.disc_no)
            t.disc_total = max(d for _, d, _ in al.tracks)
        t.date = t.date or al.date
        t.cover_url = t.cover_url or al.cover
    return t


def expand(kind: str, sid: str) -> tuple[str, list[str]]:
    """Turn a track/album/playlist link into (label, [track ids])."""
    if kind == "track":
        return "", [sid]
    if kind == "album":
        al = spotify_album(sid)
        return al.name or sid, [tid for tid, _, _ in al.tracks]
    ent = embed_entity("playlist", sid)
    ids = []
    for item in ent.get("trackList") or []:
        m = re.match(r"spotify:track:([A-Za-z0-9]{22})", item.get("uri", ""))
        if m:
            ids.append(m.group(1))
    return ent.get("name") or ent.get("title") or sid, ids


# --------------------------------------------------------------------------- #
# YouTube Music: audio source + fallback metadata
# --------------------------------------------------------------------------- #

JUNK_WORDS = (
    "karaoke", "instrumental", "cover", "remix", "live", "sped up", "slowed",
    "reverb", "nightcore", "8d", "tribute", "lullaby", "piano version",
)
MIN_SCORE = 4.5


def score_hit(t: Track, hit: dict) -> float:
    cand_title = hit.get("title", "")
    score = SequenceMatcher(None, norm(t.title), norm(cand_title)).ratio() * 4

    names = [norm(a.get("name", "")) for a in hit.get("artists") or []]
    blob = " ".join(names) + " " + norm(cand_title)
    wanted = [norm(a) for a in t.artists]
    score += 2.5 if any(w and (w in names or w in blob) for w in wanted) else -2

    dur = hit.get("duration_seconds")
    if dur and t.duration_ms:
        d = abs(dur - t.duration_ms / 1000)
        score += 3 if d <= 2 else 2 if d <= 5 else 0.5 if d <= 10 else -1.5 if d <= 30 else -3

    album = (hit.get("album") or {}).get("name")
    if album and t.album and norm(album) == norm(t.album):
        score += 1
    if hit.get("resultType") == "song":
        score += 0.5

    a, b = t.title.casefold(), cand_title.casefold()
    for w in JUNK_WORDS:
        pat = rf"\b{re.escape(w)}\b"
        if re.search(pat, b) and not re.search(pat, a):
            score -= 3
    return score


def find_match(yt, t: Track):
    query = f"{t.artists[0]} {t.title}"
    best, best_score = None, -99.0
    for flt in ("songs", "videos"):
        try:
            results = yt.search(query, filter=flt, limit=10)
        except Exception as e:  # ytmusicapi raises a zoo of exceptions
            raise PipelineError(f"YT Music search failed: {e}") from e
        for hit in results:
            if hit.get("videoId"):
                s = score_hit(t, hit)
                if s > best_score:
                    best, best_score = hit, s
        if best and best_score >= MIN_SCORE:
            break
    return best if best and best_score >= MIN_SCORE else None


def upscale_thumb(url: str) -> str:
    return re.sub(r"=w\d+-h\d+.*$", "=w640-h640-l90-rj", url)


def fill_gaps(yt, t: Track, hit: dict) -> list[str]:
    """Fill ONLY what Spotify didn't give us. Returns what came from YT Music."""
    used: list[str] = []
    alb = hit.get("album") or {}
    if not t.album and alb.get("name"):
        t.album = alb["name"]
        used.append("album")

    need_no = not t.track_no or not t.track_total
    detail = None
    if alb.get("id") and (need_no or not t.date):
        try:
            detail = yt.get_album(alb["id"])
        except Exception:
            detail = None
    if detail:
        tracks = detail.get("tracks") or []
        if need_no and tracks:
            idx = next((i for i, x in enumerate(tracks) if x.get("videoId") == hit["videoId"]), None)
            if idx is None:
                want = norm(t.title)
                idx = next((i for i, x in enumerate(tracks) if norm(x.get("title", "")) == want), None)
            if idx is not None:
                if not t.track_no:
                    t.track_no = idx + 1
                    used.append("track #")
                if not t.track_total:
                    t.track_total = len(tracks)
        if not t.date and detail.get("year"):
            t.date = str(detail["year"])
            used.append("year")
    if not t.date and hit.get("year"):
        t.date = str(hit["year"])
        used.append("year")
    if not t.cover_url and hit.get("thumbnails"):
        t.cover_url = upscale_thumb(hit["thumbnails"][-1]["url"])
        used.append("cover")
    if not t.album_artist:
        t.album_artist = t.artists[0]
    return used


def js_runtimes() -> dict:
    """yt-dlp needs a JS runtime to solve YouTube's challenges."""
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            return {name: {}}
    return {}


def clean_error(e: Exception) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()


def download_audio(video_id: str, workdir: Path, job: "Job") -> Path:
    """Download the best raw audio stream from YT Music (no conversion yet)."""
    import yt_dlp

    def on_progress(d):
        if d["status"] == "downloading":
            job.dl = (
                d.get("downloaded_bytes") or 0,
                d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                d.get("speed") or 0,
            )
        elif d["status"] == "finished":
            job.set("dl", "done", f"{fmt_bytes(job.dl[1] or job.dl[0])} from YT Music")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "progress_hooks": [on_progress],
        "js_runtimes": js_runtimes(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://music.youtube.com/watch?v={video_id}"])
    except yt_dlp.utils.DownloadError as e:
        raise PipelineError(f"download failed: {clean_error(e)}") from e
    files = [
        p for p in workdir.iterdir()
        if p.is_file() and p.name.startswith("source.") and not p.name.endswith(".part")
    ]
    if not files:
        raise PipelineError("no audio was downloaded")
    return max(files, key=lambda p: p.stat().st_mtime)


def encode_audio(raw: Path, dest: Path, fmt: AudioFormat) -> Path:
    """ffmpeg transcode into the chosen format (lossless for FLAC/WAV/ALAC)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw), "-vn",
           "-map_metadata", "-1", "-c:a", fmt.codec, *fmt.codec_args]
    if fmt.container:
        cmd += ["-f", fmt.container]
    cmd.append(str(dest))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PipelineError(f"ffmpeg failed: {e}") from e
    if r.returncode != 0 or not dest.exists():
        raise PipelineError(f"ffmpeg {fmt.name} conversion failed: {r.stderr.strip()[:300]}")
    return dest


def fetch_cover(url: str) -> bytes:
    data = get(url).content
    if not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")):
        raise PipelineError("cover wasn't a JPEG/PNG")
    return data


# --------------------------------------------------------------------------- #
# Lyrics: LRCLIB -> Genius -> YouTube Music. No API keys, works for Arabic
# nasheeds as well as ordinary songs (LRCLIB is the synced-LRC source).
# --------------------------------------------------------------------------- #

@dataclass
class Lyrics:
    text: str
    source: str
    synced: str = ""

    def __bool__(self) -> bool:
        return bool(self.text)


GENIUS_JUNK = (
    "how to format lyrics", "you might also like", "embed", "cancel",
    "read more", "learn more", "sign up", "q&a", "song bio", "about this song",
)


def clean_lyrics(raw: str, source: str = "") -> str:
    """Keep section headers ([Chorus], [المقطع الأول]) but drop page furniture."""
    if not raw:
        return ""
    text = html.unescape(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    if source == "genius":
        text = re.sub(r"^\s*\d+\s+Contributors?.*?\bLyrics\b\s*", "", text, count=1, flags=re.I | re.S)
    lines: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        low = s.casefold()
        if not s:
            lines.append("")
            continue
        if low in ("lyrics", "instrumental"):
            continue
        if source == "genius" and any(j in low for j in GENIUS_JUNK):
            continue
        lines.append(s)
    out: list[str] = []
    blanks = 0
    for s in lines:
        if s:
            if blanks and out:
                out.append("")
            out.append(s)
            blanks = 0
        else:
            blanks += 1
    return "\n".join(out).strip()


class GeniusLyricsParser(HTMLParser):
    """Collect only text inside Genius' (nested) lyrics containers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            if tag == "div":
                self.skip += 1
            return
        if tag == "div":
            if self.depth == 0:
                if a.get("data-lyrics-container") == "true":
                    self.depth = 1
                    if a.get("data-exclude-from-selection") == "true":
                        self.skip = 1
            else:
                self.depth += 1
                if a.get("data-exclude-from-selection") == "true":
                    self.skip = 1
        elif self.depth and tag in ("br", "p"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == "div":
                self.skip -= 1
            return
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.chunks.append(data)


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    return SequenceMatcher(None, a, b).ratio() * 3 if a and b else 0.0


def artist_ok(wanted: list[str], got: list[str]) -> bool:
    words = {w for a in wanted for w in norm(a).split() if len(w) > 1}
    blob = " ".join(norm(a) for a in got)
    return any(w in blob for w in words) if words else True


def lrclib_lyrics(t: Track, duration: int = 0) -> Lyrics | None:
    params = {"track_name": t.title, "artist_name": t.artists[0] if t.artists else ""}
    if t.album:
        params["album_name"] = t.album
    if duration:
        params["duration"] = str(duration)
    r = get_quiet("https://lrclib.net/api/get?" + urlencode(params))
    row = None
    if r:
        try:
            candidate = r.json()
        except ValueError:
            candidate = None
        if candidate and candidate.get("instrumental"):
            return None
        # /api/get is a best-match endpoint, not a guarantee: keep it only when
        # the returned artist and title actually agree with Spotify's metadata.
        if (
            candidate
            and artist_ok(t.artists, [candidate.get("artistName", "")])
            and similar(t.title, candidate.get("trackName") or "") >= 2.5
        ):
            row = candidate
    if not row:
        q = " ".join(([t.artists[0]] if t.artists else []) + [t.title])
        r = get_quiet("https://lrclib.net/api/search?" + urlencode({"q": q}))
        try:
            rows = r.json() if r else []
        except ValueError:
            rows = []
        best, score = None, 0.0
        for item in rows if isinstance(rows, list) else []:
            s = similar(t.title, item.get("trackName") or "")
            if artist_ok(t.artists, [item.get("artistName", "")]):
                s += 2
            if s > score:
                best, score = item, s
        row = best if score >= 4.5 else None
    if not row:
        return None
    raw = row.get("plainLyrics") or row.get("syncedLyrics") or ""
    text = clean_lyrics(raw, "lrclib")
    return Lyrics(text, "LRCLIB", row.get("syncedLyrics") or "") if len(text) >= 20 else None


def genius_lyrics(t: Track) -> Lyrics | None:
    def find_path(query: str, need_artist: bool) -> str:
        r = get_quiet("https://genius.com/api/search/multi?" + urlencode({"q": query}), lyric_http)
        if not r:
            return ""
        try:
            sections = r.json()["response"]["sections"]
        except (KeyError, ValueError, TypeError):
            return ""
        rows = [
            h["result"] for sec in sections for h in (sec.get("hits") or [])
            if h.get("type") == "song" and h.get("result") and h["result"].get("path")
        ]
        best, score = "", 0.0
        for row in rows:
            if row.get("instrumental"):
                continue
            if need_artist and not artist_ok(t.artists, [row.get("artist_names", "")]):
                continue
            s = similar(t.title, row.get("title", ""))
            if need_artist:
                s += 2
            if s > score and s >= (4.5 if need_artist else 2.7):
                best, score = row["path"], s
        return best

    # Require the artist to match. A title-only retry used to attach lyrics
    # from unrelated songs with the same name (e.g. Maher Zain's
    # "Huwa Ahmadun" to an Adzando Davema track), so it is deliberately gone.
    q = " ".join(([t.artists[0]] if t.artists else []) + [t.title])
    path = find_path(q, True)
    if not path:
        return None
    page = get_quiet("https://genius.com" + path, lyric_http)
    if not page:
        return None
    parser = GeniusLyricsParser()
    try:
        parser.feed(page.text)
    except Exception:
        pass
    raw = "\n".join(parser.chunks) if parser.chunks else ""
    if not raw.strip():
        m = re.search(r'<div class="lyrics">(.*?)</div>', page.text, re.S)
        raw = m.group(1) if m else ""
    text = clean_lyrics(raw, "genius")
    return Lyrics(text, "Genius") if len(text) >= 20 else None


def ytm_lyrics(yt, t: Track) -> Lyrics | None:
    """Last resort: only the single confident YT Music match for this track."""
    try:
        hit = find_match(yt, t)
        if not hit:
            return None
        browse_id = (yt.get_watch_playlist(hit["videoId"]) or {}).get("lyrics")
        if not browse_id:
            return None
        raw = (yt.get_lyrics(browse_id) or {}).get("lyrics") or ""
        text = clean_lyrics(raw, "ytmusic")
        return Lyrics(text, "YT Music") if len(text) >= 20 else None
    except Exception:
        return None


def fetch_lyrics(yt, t: Track, duration: int = 0) -> Lyrics | None:
    """First source with real lyrics wins; LRCLIB gives synced LRC when possible."""
    for fn in (lrclib_lyrics, genius_lyrics):
        try:
            got = fn(t, duration) if fn is lrclib_lyrics else fn(t)
            if got:
                return got
        except Exception:
            pass
    return ytm_lyrics(yt, t)


def apply_lyrics(f: FLAC, lyrics: Lyrics) -> None:
    f["LYRICS"] = lyrics.text
    f["UNSYNCEDLYRICS"] = lyrics.text
    if lyrics.synced:
        f["SYNCEDLYRICS"] = lyrics.synced


def tag_flac(path: Path, t: Track, cover: bytes | None, lyrics: Lyrics | None = None,
             source_link: str = "") -> None:
    f = FLAC(path)
    f.clear_pictures()
    f["TITLE"] = t.title
    f["ARTIST"] = artist_label(t.artists)
    f["ALBUMARTIST"] = t.album_artist or t.artists[0]
    if t.album:
        f["ALBUM"] = t.album
    if t.track_no:
        f["TRACKNUMBER"] = str(t.track_no)
    if t.track_total:
        f["TRACKTOTAL"] = str(t.track_total)
        f["TOTALTRACKS"] = str(t.track_total)
    if t.disc_no:
        f["DISCNUMBER"] = str(t.disc_no)
    if t.disc_total:
        f["DISCTOTAL"] = str(t.disc_total)
    if t.date:
        f["DATE"] = t.date
    source = source_link or (f"https://open.spotify.com/track/{t.id}" if t.id else "")
    if source:
        f["SOURCE"] = source  # canonical link, read back by --update-metadata
        f["COMMENT"] = source  # older readers still understand this
    if lyrics:
        apply_lyrics(f, lyrics)
    if cover:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = "image/png" if cover.startswith(b"\x89PNG") else "image/jpeg"
        pic.desc = "Cover"
        pic.data = cover
        f.add_picture(pic)
    f.save()


def cover_picture(cover: bytes) -> Picture:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = "image/png" if cover.startswith(b"\x89PNG") else "image/jpeg"
    pic.desc = "Cover"
    pic.data = cover
    return pic


def set_cover_generic(path: Path, cover: bytes) -> bool:
    """Best-effort cover embedding for formats other than FLAC."""
    try:
        f = mutagen.File(path)
        if f is None:
            return False
        pic = cover_picture(cover)
        if isinstance(f, FLAC):
            f.clear_pictures()
            f.add_picture(pic)
        elif isinstance(f, MP4):
            kind = MP4Cover.FORMAT_PNG if pic.mime == "image/png" else MP4Cover.FORMAT_JPEG
            f["covr"] = [MP4Cover(cover, imageformat=kind)]
        elif isinstance(f, (OggVorbis, OggOpus)):
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        else:
            tags = getattr(f, "tags", None)
            if tags is None and hasattr(f, "add_tags"):
                try:
                    tags = f.add_tags()
                except Exception:
                    tags = None
            if isinstance(tags, ID3):
                tags.setall("APIC", [APIC(encoding=3, mime=pic.mime, type=3, desc="Cover", data=cover)])
            else:
                return False
        f.save()
        return True
    except Exception:
        return False


def tag_generic(path: Path, t: Track, cover: bytes | None, lyrics: Lyrics | None,
                source_link: str = "") -> None:
    """Tag any mutagen-supported format; unsupported keys are simply skipped."""
    f = mutagen.File(path, easy=True)
    if f is None:
        return
    source = source_link or (f"https://open.spotify.com/track/{t.id}" if t.id else "")
    values = [
        ("title", t.title),
        ("artist", artist_label(t.artists)),
        ("albumartist", t.album_artist or (t.artists[0] if t.artists else "")),
        ("album", t.album),
        ("tracknumber", str(t.track_no) if t.track_no else ""),
        ("tracktotal", str(t.track_total) if t.track_total else ""),
        ("totaltracks", str(t.track_total) if t.track_total else ""),
        ("discnumber", str(t.disc_no) if t.disc_no else ""),
        ("disctotal", str(t.disc_total) if t.disc_total else ""),
        ("date", t.date),
        ("comment", source),
        ("SOURCE", source),
        ("lyrics", lyrics.text if lyrics else ""),
        ("UNSYNCEDLYRICS", lyrics.text if lyrics else ""),
        ("SYNCEDLYRICS", lyrics.synced if lyrics else ""),
    ]
    for key, val in values:
        if not val:
            continue
        for attempt in (str(val), [str(val)]):
            try:
                f[key] = attempt
                break
            except Exception:
                continue
    try:
        f.save()
    except Exception:
        pass
    if cover:
        set_cover_generic(path, cover)


def tag_id3(path: Path, t: Track, cover: bytes | None, lyrics: Lyrics | None,
            source_link: str = "") -> None:
    """Explicit ID3 frames — mutagen's EasyID3 key list is too narrow here."""
    f = mutagen.File(path)
    if f is None:
        return
    tags = getattr(f, "tags", None)
    if tags is None:
        try:
            tags = f.add_tags()
        except Exception:
            return
    source = source_link or (f"https://open.spotify.com/track/{t.id}" if t.id else "")

    def frame(name, text):
        if text:
            tags.setall(name, [TXXX(encoding=3, desc=name, text=[str(text)])])

    for name, text in (
        ("TIT2", t.title), ("TPE1", artist_label(t.artists)),
        ("TPE2", t.album_artist or (t.artists[0] if t.artists else "")),
        ("TALB", t.album), ("TDRC", t.date),
        ("TRCK", f"{t.track_no}/{t.track_total}" if t.track_no and t.track_total else str(t.track_no or "")),
        ("TPOS", f"{t.disc_no}/{t.disc_total}" if t.disc_no and t.disc_total else str(t.disc_no or "")),
    ):
        if text:
            cls = {"TIT2": TIT2, "TPE1": TPE1, "TPE2": TPE2, "TALB": TALB, "TDRC": TDRC,
                   "TRCK": TRCK, "TPOS": TPOS}[name]
            tags.setall(name, [cls(encoding=3, text=[str(text)])])
    if source:
        tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=[source])])
        frame("SOURCE", source)
    if lyrics:
        tags.setall("USLT", [USLT(encoding=3, lang="eng", desc="", text=lyrics.text)])
        frame("UNSYNCEDLYRICS", lyrics.text)
        if lyrics.synced:
            frame("SYNCEDLYRICS", lyrics.synced)
    if cover:
        pic = cover_picture(cover)
        tags.setall("APIC", [APIC(encoding=3, mime=pic.mime, type=3, desc="Cover", data=cover)])
    f.save()


def tag_audio(path: Path, t: Track, cover: bytes | None, lyrics: Lyrics | None,
              source_link: str = "") -> None:
    ext = path.suffix.lower()
    if ext == ".flac":
        tag_flac(path, t, cover, lyrics, source_link)
    elif ext in (".mp3", ".wav", ".aiff"):
        tag_id3(path, t, cover, lyrics, source_link)
    else:
        tag_generic(path, t, cover, lyrics, source_link)


def track_artist(t: Track) -> str:
    """Album artist for the folder, falling back to the first credited artist."""
    return t.album_artist or (t.artists[0] if t.artists else "") or "Unknown Artist"


def folder_label(dev: Device, path: Path) -> str:
    """How an album folder is shown in the TUI: Artist/Album."""
    try:
        return str(path.parent.relative_to(dev.library))
    except ValueError:
        return path.parent.name


def album_folder(t: Track) -> str:
    """Singles are their own album: no album tag, or only one track in it."""
    if not t.album or t.track_total == 1:
        return "Singles"
    return safe_name(t.album, "Singles")


def target_path(dev: Device, t: Track) -> Path:
    artist = safe_name(track_artist(t), "Unknown Artist")
    album = album_folder(t)
    title = safe_name(t.title, "Track")
    ext = ACTIVE_FORMAT.ext
    if t.track_no and album != "Singles":
        prefix = f"{t.disc_no}-{t.track_no:02d}" if t.disc_total > 1 else f"{t.track_no:02d}"
        name = f"{prefix} - {title}{ext}"
    else:
        name = f"{title}{ext}"
    return dev.library / artist / album / name


# --------------------------------------------------------------------------- #
# pipeline + TUI
# --------------------------------------------------------------------------- #

UPDATE_STEPS = [
    ("meta", "Spotify link"),
    ("lyrics", "Lyrics"),
    ("tag", "Write tags"),
]


class Job:
    def __init__(self, idx: int, total: int, sid: str, steps=STEPS):
        self.idx, self.total, self.sid = idx, total, sid
        self.title = f"spotify:track:{sid}"
        self.steps = {k: ("pending", "") for k, _ in steps}
        self.dl = (0, 0, 0.0)

    def set(self, key: str, status: str, detail="") -> None:
        self.steps[key] = (status, detail)


@dataclass
class Result:
    status: str  # ok | skip | fail
    label: str
    folder: str = ""
    note: str = ""
    size: int = 0
    no_lyrics: bool = False
    no_source: bool = False
    source_added: bool = False
    source_refreshed: bool = False
    cover_refreshed: bool = False
    lyrics_source: str = ""
    artist: str = ""
    album: str = ""
    title: str = ""


def process_track(job: Job, dev: Device, yt, workdir: Path) -> Result:
    # 1. Spotify metadata -------------------------------------------------
    job.set("meta", "active")
    t = spotify_track(job.sid)
    job.title = f"{artist_label(t.artists)} — {t.title}"
    year = f" ({t.date[:4]})" if t.date else ""
    meta_detail = f"{t.album or 'no album listed'}{year}"
    job.set("meta", "done", meta_detail)

    def already_there() -> Path | None:
        """Return an existing copy of this song, or None if it must be fetched."""
        dest = target_path(dev, t)
        if dest.exists():
            return dest
        # The folder may already be on the device from an earlier run that had
        # different track numbering (or none). Match on the title in that case
        # so the song isn't downloaded twice.
        folder = dest.parent
        if folder.is_dir():
            wanted = safe_name(t.title, "Track").casefold()
            for f in folder.iterdir():
                if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
                    continue
                stem = re.sub(r"^(?:\d+-)?\d{1,3} - ", "", f.stem)
                if stem.casefold() == wanted:
                    return f
        return None

    def skipped(p: Path) -> Result:
        for k in ("match", "dl", "encode", "tag", "lyrics", "copy"):
            job.set(k, "skip")
        job.set("copy", "skip", f"already in {folder_label(dev, p)}")
        return Result("skip", job.title, folder_label(dev, p), "already on device")

    if (p := already_there()) is not None:
        return skipped(p)

    # 2. YT Music match + gap filling ---------------------------------------
    job.set("match", "active")
    hit = find_match(yt, t)
    if not hit:
        raise PipelineError("no confident match on YT Music")
    artists = ", ".join(a["name"] for a in hit.get("artists") or [] if a.get("name"))
    dur = f" · {fmt_time(hit['duration_seconds'])}" if hit.get("duration_seconds") else ""
    job.set("match", "done", f"{hit.get('title', '')} — {artists}{dur}")

    used = fill_gaps(yt, t, hit)
    if used:
        job.set(
            "meta", "done",
            Text.assemble((meta_detail, SUB), ("   ↳ from YT Music: " + ", ".join(used), YELLOW)),
        )
    if (p := already_there()) is not None:
        return skipped(p)

    # 3+4. download + transcode ----------------------------------------------
    job.set("dl", "active")
    raw = download_audio(hit["videoId"], workdir, job)
    job.set("encode", "active", f"ffmpeg → {ACTIVE_FORMAT.name}")
    audio = encode_audio(raw, workdir / f"audio{ACTIVE_FORMAT.ext}", ACTIVE_FORMAT)
    job.set("encode", "done", fmt_bytes(audio.stat().st_size))

    # 5. lyrics + tag + cover ---------------------------------------------------
    job.set("lyrics", "active")
    lyrics = fetch_lyrics(yt, t, round(t.duration_ms / 1000) if t.duration_ms else 0)
    job.set(
        "lyrics", "done" if lyrics else "skip",
        f"{lyrics.source} · {len(lyrics.text)} chars" if lyrics else "none found",
    )
    job.set("tag", "active")
    cover, cover_note = None, ""
    if t.cover_url:
        try:
            cover = fetch_cover(t.cover_url)
        except PipelineError:
            cover = None
    if cover is None and hit.get("thumbnails"):
        try:
            cover = fetch_cover(upscale_thumb(hit["thumbnails"][-1]["url"]))
            cover_note = "   ↳ cover from YT Music"
        except PipelineError:
            cover = None
    tag_audio(audio, t, cover, lyrics)
    job.set(
        "tag", "done",
        Text.assemble(
            (f"cover {fmt_bytes(len(cover))}" if cover else "no cover found", SUB),
            (cover_note, YELLOW),
        ),
    )

    # 6. copy ------------------------------------------------------------------
    job.set("copy", "active")
    dest = target_path(dev, t)
    size = audio.stat().st_size
    if not dev.present():
        raise PipelineError("device went away")
    usage = dev.usage()
    if usage and usage.free < size + 5 * 1024 * 1024:
        raise PipelineError("not enough space on the device")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name("." + dest.name + ".part")
    try:
        shutil.copyfile(audio, part)
        os.replace(part, dest)
    except OSError as e:
        part.unlink(missing_ok=True)
        keep = Path.home() / ".cache" / "music2dap" / "pending"
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audio, keep / dest.name)
        raise PipelineError(f"copy failed ({e.strerror}); kept at {keep / dest.name}") from e
    folder = folder_label(dev, dest)
    job.set("copy", "done", f"{folder}/{dest.name}")
    note = "from YT Music: " + ", ".join(used) if used else ""
    if not lyrics:
        note = (note + " · " if note else "") + "no lyrics found (LRCLIB, Genius, YT Music)"
    return Result("ok", job.title, folder, note, size, no_lyrics=not lyrics)


class Screen:
    def __init__(self, dev: Device | None, total: int, steps=STEPS):
        self.dev = dev
        self.step_defs = steps
        self.job: Job | None = None
        self.results: list[Result] = []
        self.progress = Progress(
            TextColumn("[bold]overall[/]"),
            BarColumn(bar_width=None, style=SURFACE, complete_style=MAUVE, finished_style=GREEN),
            TaskProgressColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
            expand=True,
        )
        self.task = self.progress.add_task("", total=total)
        self.spinners = {k: Spinner("dots", style=MAUVE) for k, _ in steps}

    def dl_bar(self, job: Job) -> Text:
        done, total, speed = job.dl
        width = 26
        frac = min(done / total, 1) if total else 0
        filled = int(frac * width)
        bar = Text("━" * filled, style=MAUVE) + Text("━" * (width - filled), style=SURFACE)
        info = f"  {frac * 100:3.0f}%  {fmt_bytes(done)} / {fmt_bytes(total)}"
        if speed:
            info += f"  {fmt_bytes(speed)}/s"
        if not total:
            info = f"  {fmt_bytes(done)}"
        return bar + Text(info, style=SUB)

    def job_panel(self) -> Panel:
        job = self.job
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=2)
        grid.add_column(min_width=17)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        for key, label in self.step_defs:
            status, detail = job.steps[key]
            if status == "done":
                icon = Text("✓", style=GREEN)
            elif status == "active":
                icon = self.spinners[key]
            elif status == "fail":
                icon = Text("✗", style=RED)
            elif status == "skip":
                icon = Text("↷", style=YELLOW)
            else:
                icon = Text("○", style=DIM)
            lab = Text(label, style=TEXT if status in ("done", "active") else DIM)
            if key == "dl" and status == "active":
                det = self.dl_bar(job)
            elif isinstance(detail, Text):
                det = detail
            else:
                det = Text(detail, style=RED if status == "fail" else SUB)
            grid.add_row(icon, lab, det)
        title = Text.assemble(
            (f" {job.idx}/{job.total} ", f"bold {BASE} on {MAUVE}"),
            ("  ♪ ", MAUVE),
            (job.title, f"bold {TEXT}"),
        )
        return Panel(grid, title=title, title_align="left", border_style=MAUVE, box=box.ROUNDED, padding=(1, 2))

    def recent(self):
        if not self.results:
            return Text("")
        table = Table(box=box.SIMPLE_HEAD, border_style=SURFACE, header_style=DIM, expand=True, pad_edge=False)
        table.add_column("", width=2)
        table.add_column("track", ratio=3, no_wrap=True, overflow="ellipsis")
        table.add_column("album folder", ratio=2, no_wrap=True, overflow="ellipsis")
        table.add_column("note", ratio=2, no_wrap=True, overflow="ellipsis")
        for r in self.results[-6:]:
            icon, style = {"ok": ("✓", GREEN), "skip": ("↷", YELLOW), "fail": ("✗", RED)}[r.status]
            table.add_row(
                Text(icon, style=style),
                Text(r.label, style=TEXT if r.status != "skip" else DIM),
                Text(r.folder, style=SUB),
                Text(r.note, style=RED if r.status == "fail" else DIM),
            )
        return table

    def __call__(self):
        parts = [banner(self.dev)] if self.dev else []
        parts.append(self.progress)
        if self.job:
            parts.append(self.job_panel())
        parts.append(self.recent())
        return Group(*parts)


def summary(results: list[Result], elapsed: float, interrupted: bool, verb: str = "copied") -> Panel:
    ok = [r for r in results if r.status == "ok"]
    skipped = [r for r in results if r.status == "skip"]
    failed = [r for r in results if r.status == "fail"]
    no_lyrics = [r for r in ok if r.no_lyrics]
    no_source = [r for r in ok if r.no_source]
    line = Text()
    line.append(f"✓ {len(ok)} {verb}", style=f"bold {GREEN}")
    if skipped:
        line.append(f"   ↷ {len(skipped)} already there", style=YELLOW)
    if no_source:
        line.append(f"   ! {len(no_source)} without Spotify URL", style=f"bold {YELLOW}")
    if no_lyrics:
        line.append(f"   ! {len(no_lyrics)} without lyrics", style=f"bold {YELLOW}")
    if failed:
        line.append(f"   ✗ {len(failed)} failed", style=f"bold {RED}")
    line.append(f"   · {fmt_bytes(sum(r.size for r in ok))} in {fmt_time(elapsed)}", style=DIM)
    body: list = [line]
    if interrupted:
        body.append(Text("stopped early (ctrl-c)", style=YELLOW))
    if failed:
        body.append(Text(""))
        for r in failed:
            body.append(Text.assemble(("✗ ", RED), (r.label, TEXT), ("\n   " + r.note, DIM)))
    if no_source:
        body.append(Text(""))
        for r in no_source:
            body.append(Text.assemble(
                ("! ", YELLOW), (r.label, TEXT),
                ("\n   no matching Spotify song found (SOURCE left empty)", DIM),
            ))
    if no_lyrics:
        body.append(Text(""))
        for r in no_lyrics:
            body.append(Text.assemble(
                ("! ", YELLOW), (r.label, TEXT),
                ("\n   no lyrics found (LRCLIB, Genius, YT Music)", DIM),
            ))
    if ok and verb == "copied":
        body.append(Text(""))
        body.append(
            Text(
                "Writes are flushed. If new songs don't show on the R1, run a Library Rescan in Music Settings.",
                style=DIM,
            )
        )
    colour = RED if failed and not ok else YELLOW if failed or no_lyrics or no_source or interrupted else GREEN
    return Panel(
        Group(*body),
        title=Text(" done ", style=f"bold {BASE} on {colour}"),
        title_align="left",
        border_style=colour,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def update_summary(results: list[Result], elapsed: float, interrupted: bool) -> Panel:
    """Detailed report for --update-metadata: counts + every track's changes."""
    ok = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status == "fail"]
    no_source = [r for r in ok if r.no_source]
    no_lyrics = [r for r in ok if r.no_lyrics]
    with_source = [r for r in ok if not r.no_source]

    added = sum(r.source_added for r in ok)
    refreshed = sum(r.source_refreshed for r in ok)
    unchanged = max(0, len(with_source) - added - refreshed)
    covers = sum(r.cover_refreshed for r in ok)
    lyrics = Counter(r.lyrics_source for r in ok if r.lyrics_source)

    line = Text()
    line.append(f"✓ {len(ok)} updated", style=f"bold {GREEN}")
    if failed:
        line.append(f"   ✗ {len(failed)} failed", style=f"bold {RED}")
    line.append(f"   · {fmt_time(elapsed)}", style=DIM)

    body: list = [line, Text("")]
    body.append(Text.assemble(
        ("SOURCE   ", SUB),
        (f"{added} added", GREEN if added else DIM), ("   "),
        (f"{refreshed} refreshed", GREEN if refreshed else DIM), ("   "),
        (f"{unchanged} unchanged", DIM),
    ))
    if no_source:
        body[-1].append(f"   {len(no_source)} not found", style=f"bold {YELLOW}")

    lyric_line = Text.assemble(("Lyrics    ", SUB))
    if lyrics:
        for i, (source, count) in enumerate(sorted(lyrics.items())):
            if i:
                lyric_line.append("   ", style=DIM)
            lyric_line.append(f"{count} {source}", style=TEXT)
    else:
        lyric_line.append("none found", style=DIM)
    if no_lyrics:
        lyric_line.append(f"   {len(no_lyrics)} missing", style=f"bold {YELLOW}")
    body.append(lyric_line)
    body.append(Text.assemble(("Cover     ", SUB), (f"{covers} refreshed", GREEN if covers else DIM)))

    if interrupted:
        body.append(Text(""))
        body.append(Text("stopped early (ctrl-c)", style=YELLOW))

    attention = failed + no_source + no_lyrics
    if attention:
        body.append(Text(""))
        body.append(Text("needs attention", style=f"bold {YELLOW}"))
        for r in attention:
            mark = "✗" if r.status == "fail" else "!"
            colour = RED if r.status == "fail" else YELLOW
            body.append(Text.assemble((f"  {mark} ", colour), (r.label, TEXT),
                                      ("  — " + r.note if r.note else "", DIM)))

    body.append(Text(""))
    tree = Tree(Text("tracks", style=f"bold {SUB}"))
    grouped: dict[tuple[str, str], list[Result]] = {}
    for r in results:
        grouped.setdefault((r.album or "Singles", r.artist or "Unknown Artist"), []).append(r)
    for (album, artist), tracks in sorted(grouped.items(), key=lambda x: (x[0][0].casefold(), x[0][1].casefold())):
        album_node = tree.add(Text(f"{album} ({artist})", style=f"bold {MAUVE}"))
        for r in tracks:
            if r.status == "fail":
                mark, colour = "✗", RED
            elif r.no_source or r.no_lyrics:
                mark, colour = "!", YELLOW
            else:
                mark, colour = "✓", GREEN
            title = r.title or r.label
            song = album_node.add(Text.assemble((f"{mark} ", colour), (title, TEXT)))
            song.add(Text(r.note, style=DIM))
    body.append(tree)

    colour = RED if failed and not ok else YELLOW if failed or no_lyrics or no_source or interrupted else GREEN
    return Panel(
        Group(*body),
        title=Text(" update summary ", style=f"bold {BASE} on {colour}"),
        title_align="left",
        border_style=colour,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def collect_urls() -> list[tuple[str, str]]:
    console.print(
        Text.assemble(
            ("\n  Paste Spotify links", f"bold {TEXT}"),
            (" (track, album or playlist), one per line.\n  Type ", SUB),
            ("done", f"bold {MAUVE}"),
            (" when you're finished.\n", SUB),
        )
    )
    items: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    prompt = Text("  ❯ ", style=f"bold {MAUVE}")
    while True:
        try:
            line = console.input(prompt)
        except EOFError:
            return items
        except KeyboardInterrupt:
            console.print()
            raise SystemExit(130)
        for token in line.split():
            low = token.lower()
            if low in ("done", "go"):
                return items
            if low in ("q", "quit", "exit"):
                raise SystemExit(0)
            parsed = parse_spotify(token)
            if not parsed:
                console.print(Text(f"  ✗ not a Spotify track/album/playlist link: {token[:60]}", style=RED))
            elif parsed in seen:
                console.print(Text("  ↷ already added", style=YELLOW))
            else:
                seen.add(parsed)
                items.append(parsed)
                console.print(
                    Text.assemble(
                        ("  ✓ ", GREEN),
                        (parsed[0].ljust(9), TEXT),
                        (parsed[1], DIM),
                        (f"   {len(items)} queued", DIM),
                    )
                )


def expand_all(items: list[tuple[str, str]]) -> list[str]:
    ids: list[str] = []
    for kind, sid in items:
        with console.status(Text(f"reading {kind} {sid}…", style=SUB), spinner="dots", spinner_style=MAUVE):
            try:
                label, found = expand(kind, sid)
            except PipelineError as e:
                console.print(Text(f"  ✗ {kind} {sid}: {e}", style=RED))
                continue
        if not found:
            console.print(Text(f"  ✗ {kind} {sid}: no tracks found", style=RED))
            continue
        if kind != "track":
            console.print(Text.assemble(("  ✓ ", GREEN), (f"{kind} “{label}”", TEXT), (f"  {len(found)} tracks", DIM)))
        ids.extend(found)
    return list(dict.fromkeys(ids))


def run_queue(dev: Device, ids: list[str]) -> None:
    from ytmusicapi import YTMusic

    yt = YTMusic()
    steps = build_steps(ACTIVE_FORMAT)
    screen = Screen(dev, len(ids), steps)
    started = time.time()
    interrupted = False
    with Live(get_renderable=screen, console=console, refresh_per_second=12, transient=True):
        try:
            for i, sid in enumerate(ids, 1):
                job = Job(i, len(ids), sid, steps)
                screen.job = job
                with tempfile.TemporaryDirectory(prefix="music2dap-") as tmp:
                    try:
                        res = process_track(job, dev, yt, Path(tmp))
                    except PipelineError as e:
                        res = Result("fail", job.title, "", str(e))
                    except Exception as e:  # keep the queue going no matter what
                        res = Result("fail", job.title, "", f"{type(e).__name__}: {e}")
                if res.status == "fail":
                    for key, (status, _) in job.steps.items():
                        if status == "active":
                            job.set(key, "fail", res.note)
                screen.results.append(res)
                screen.progress.advance(screen.task)
                if not console.is_terminal:
                    mark = {"ok": "ok  ", "skip": "skip", "fail": "FAIL"}[res.status]
                    console.print(f"[{i}/{len(ids)}] {mark} {res.label} {res.folder} {res.note}", highlight=False)
        except KeyboardInterrupt:
            interrupted = True
    with console.status(Text("flushing writes to the device…", style=SUB), spinner="dots", spinner_style=MAUVE):
        os.sync()
    console.print(summary(screen.results, time.time() - started, interrupted))
    if any(r.status == "fail" for r in screen.results):
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# --update-metadata: refresh tags + lyrics in files that are already on disk
# --------------------------------------------------------------------------- #

_source_search_cache: dict[str, str] = {}

# Spotify's anonymous web-player search (same endpoint the web UI uses).
# Hash rotates rarely; if it ever does, the legacy crawl below still runs.
_PATHFINDER_HASH = "eff59fa0a3d026b88b56fddbcf4bdfa16a186b8175a5c1a358c072e053c2e5b0"
_web_token = ("", 0.0)


def spotify_web_token() -> str:
    """Anonymous bearer from a public embed page; no API key or login."""
    global _web_token
    token, expiry = _web_token
    if token and time.time() < expiry - 60:
        return token
    sid = next(iter(_seed_ids), "4yuYK900ivfBnK4RwKk5Mo")
    page = get(f"https://open.spotify.com/embed/track/{sid}").text
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        raise SpotifyError("couldn't get Spotify web token")
    session = json.loads(m.group(1))["props"]["pageProps"]["state"]["settings"]["session"]
    token = session["accessToken"]
    expiry = session.get("accessTokenExpirationTimestampMs", 0) / 1000
    _web_token = (token, expiry)
    return token


def spotify_candidates(title: str, artists: list[str], album: str = "") -> list[tuple[str, str, list[str], str]]:
    """Search Spotify's public catalogue: [(id, title, artists, album), ...]."""
    token = spotify_web_token()
    queries = [" ".join(x for x in (title, " ".join(artists)) if x).strip()]
    if album and norm(album) not in norm(title):
        queries.append(f"{title} {album}".strip())
    if artists:
        queries.append(title)

    out: list[tuple[str, str, list[str], str]] = []
    seen: set[str] = set()
    for query in dict.fromkeys(queries):
        variables = {
            "searchTerm": query,
            "offset": 0,
            "limit": 10,
            "numberOfTopResults": 5,
            "includeAudiobooks": False,
            "includePreReleases": True,
            "includeAlbumPreReleases": False,
        }
        params = {
            "operationName": "searchDesktop",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": _PATHFINDER_HASH}},
                separators=(",", ":"),
            ),
        }
        url = "https://api-partner.spotify.com/pathfinder/v1/query?" + urlencode(params)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "app-platform": "WebPlayer",
            "User-Agent": "Mozilla/5.0",
        }
        r = http.get(url, headers=headers, timeout=25)
        if not r.ok:
            continue
        data = r.json().get("data", {}).get("searchV2", {})
        for item in (data.get("tracksV2") or {}).get("items") or []:
            hit = (item.get("item") or {}).get("data") or {}
            m = re.fullmatch(r"spotify:track:([A-Za-z0-9]{22})", hit.get("uri", ""))
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            names = [
                a.get("profile", {}).get("name", "")
                for a in (hit.get("artists") or {}).get("items") or []
            ]
            alb = (hit.get("albumOfTrack") or {}).get("name", "")
            out.append((m.group(1), hit.get("name", ""), [n for n in names if n], alb))
        if out:
            break
    return out


def _candidate_score(title: str, artists: list[str], album: str,
                     cand_title: str, cand_artists: list[str], cand_album: str) -> float:
    score = max(similar(title, cand_title), similar(_bare_title(title), _bare_title(cand_title)))
    if artists:
        score += 3 if artist_ok(artists, cand_artists) else -3
    if album and similar(album, cand_album) >= 2:
        score += 1
    return score


def ask_spotify_candidate(title: str, choices: list[tuple[str, str, list[str], str]]) -> str:
    """No usable artist tag: let the user pick the right one."""
    lines = [Text(f"Several Spotify matches for “{title}” — pick one:", style=SUB)]
    for i, (_, ct, ca, cal) in enumerate(choices, 1):
        lines.append(Text.assemble(
            (f"  {i}. ", MAUVE), (ct, TEXT),
            (" — ", DIM), (artist_label(ca) or "?", TEXT),
            (f"  [{cal}]" if cal else "", DIM),
        ))
    console.print(Panel(Group(*lines), title=Text(" spotify match ", style=f"bold {BASE} on {MAUVE}"),
                        title_align="left", border_style=MAUVE, box=box.ROUNDED, padding=(1, 2)))
    if not console.is_terminal or not sys.stdin.isatty():
        return choices[0][0]  # never hang in a pipe / CI
    while True:
        answer = console.input(Text("  number (blank = first, q = skip) ", style=SUB))
        answer = answer.strip()
        if answer.lower() in ("q", "quit", "skip"):
            return ""
        if not answer:
            return choices[0][0]
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1][0]


def spotify_catalog_search(title: str, artists: list[str], album: str = "") -> str:
    candidates = spotify_candidates(title, artists, album)
    if not candidates:
        return ""
    scored = [
        (_candidate_score(title, artists, album, ct, ca, cal), sid, ct, ca, cal)
        for sid, ct, ca, cal in candidates
    ]
    if artists:
        # A real artist tag is enough to decide; prefer album, then best score.
        usable = [x for x in scored if x[0] >= 4.4]
        if not usable:
            return ""
        usable.sort(key=lambda x: x[0], reverse=True)
        return usable[0][1]
    usable = [x for x in scored if x[0] >= 2.8]
    if not usable:
        return ""
    if len(usable) == 1:
        return usable[0][1]
    return ask_spotify_candidate(title, [(x[1], x[2], x[3], x[4]) for x in usable[:8]])


def source_id(raw: str) -> str:
    parsed = parse_spotify(raw) if raw else None
    return parsed[1] if parsed and parsed[0] == "track" else ""


def _bare_title(value: str) -> str:
    """'Assalamu Alayka (Arabic)' -> 'assalamu alayka'."""
    return norm(re.sub(r"\s*[\(\[].*?[\)\]]", "", value))


_seed_ids: set[str] = set()
_artist_index: dict[str, dict[str, str]] = {}
_artist_names: dict[str, set[str]] = {}
_index_done = False


def register_source_seeds(files: list[Path]) -> None:
    """Existing SOURCE links seed the catalogue lookup for the whole library."""
    for path in files:
        try:
            f = mutagen.File(path)
        except Exception:
            continue
        if f is None:
            continue
        sid = source_id(tag_value(f, "SOURCE") or tag_value(f, "COMMENT"))
        if sid:
            _seed_ids.add(sid)


def _artist_ids_for(sid: str) -> list[str]:
    try:
        ent = embed_entity("track", sid)
    except Exception:
        return []
    ids = []
    for a in ent.get("artists") or []:
        m = re.match(r"spotify:artist:([A-Za-z0-9]{22})", a.get("uri", ""))
        if m:
            ids.append(m.group(1))
    return ids


def build_artist_index() -> None:
    """Crawl public Spotify pages for every artist we already know about.

    No API key and no search engines: one known track gives the artist id, the
    artist page lists the albums, and each album's embed page lists its tracks.
    All of it is cached in memory for the run.
    """
    global _index_done
    if _index_done:
        return
    _index_done = True
    for sid in list(_seed_ids)[:60]:
        for aid in _artist_ids_for(sid):
            bucket = _artist_index.setdefault(aid, {})
            names = _artist_names.setdefault(aid, set())
            try:
                ent = embed_entity("artist", aid)
                if ent.get("name"):
                    names.add(norm(ent["name"]))
                for item in ent.get("trackList") or []:
                    m = re.match(r"spotify:track:([A-Za-z0-9]{22})", item.get("uri", ""))
                    if m and item.get("title"):
                        bucket.setdefault(norm(item["title"]), m.group(1))
                        bucket.setdefault(_bare_title(item["title"]), m.group(1))
            except Exception:
                pass
            try:
                page = get(f"https://open.spotify.com/artist/{aid}").text
                album_ids = list(dict.fromkeys(re.findall(r"/album/([A-Za-z0-9]{22})", page)))
            except Exception:
                album_ids = []
            for alb in album_ids[:60]:
                try:
                    aent = embed_entity("album", alb)
                except Exception:
                    continue
                for item in aent.get("trackList") or []:
                    m = re.match(r"spotify:track:([A-Za-z0-9]{22})", item.get("uri", ""))
                    if m and item.get("title"):
                        bucket.setdefault(norm(item["title"]), m.group(1))
                        bucket.setdefault(_bare_title(item["title"]), m.group(1))


def spotify_search_id_fallback(title: str, artists: list[str], album: str = "") -> str:
    """Old artist-crawl + web-search lookup, kept as a fallback."""
    key = norm(title) + "|" + ",".join(norm(a) for a in artists)
    if key in _source_search_cache:
        return _source_search_cache[key]

    build_artist_index()
    found = ""
    for artist in artists or [""]:
        na = norm(artist)
        for aid, names in _artist_names.items():
            if not (na in names or any(similar(na, n) >= 2.4 for n in names)):
                continue
            bucket = _artist_index[aid]
            for candidate in (norm(title), _bare_title(title)):
                if candidate in bucket:
                    found = bucket[candidate]
                    break
            if not found:
                best, score = "", 0.0
                for name, tid in bucket.items():
                    sc = similar(_bare_title(title), name)
                    if sc > score:
                        best, score = tid, sc
                if score >= 2.4:
                    found = best
            if found:
                break
        if found:
            break

    if not found:
        q = f"site:open.spotify.com/track {artists[0] if artists else ''} {title}"
        link_re = r"open\.spotify\.com/(?:intl-[^/]+/)?track/([A-Za-z0-9]{22})"
        ids: list[str] = []
        for base, params in (
            ("https://www.bing.com/search", {"format": "rss", "q": q}),
            ("https://html.duckduckgo.com/html/", {"q": q}),
        ):
            r = get_quiet(base + "?" + urlencode(params), lyric_http)
            if r:
                ids = list(dict.fromkeys(re.findall(link_re, html.unescape(r.text))))
            if ids:
                break
        best, score = "", 0.0
        for sid in ids[:12]:
            try:
                cand = spotify_track(sid)
            except Exception:
                continue
            sc = similar(title, cand.title)
            if artist_ok(artists, cand.artists):
                sc += 2
            if sc > score:
                best, score = sid, sc
        found = best if score >= 4.5 else ""

    _source_search_cache[key] = found
    return found


def spotify_search_id(title: str, artists: list[str], album: str = "") -> str:
    """Primary: anonymous Spotify search. Fallback: the old artist crawl."""
    key = norm(title) + "|" + ",".join(norm(a) for a in artists)
    if key in _source_search_cache:
        return _source_search_cache[key]
    found = ""
    try:
        found = spotify_catalog_search(title, artists, album)
    except Exception:
        found = ""
    if not found:
        found = spotify_search_id_fallback(title, artists, album)
    _source_search_cache[key] = found
    return found


ID3_KEYS = {
    "TITLE": "TIT2", "ARTIST": "TPE1", "ALBUM": "TALB", "ALBUMARTIST": "TPE2",
    "DATE": "TDRC", "TRACKNUMBER": "TRCK", "DISCNUMBER": "TPOS",
    "SOURCE": "TXXX:SOURCE", "COMMENT": "COMM", "LYRICS": "USLT",
}


def tag_value(f, key: str) -> str:
    tags = f if isinstance(f, ID3) else (getattr(f, "tags", None) or f)
    if tags is None:
        return ""
    if isinstance(tags, ID3):
        wanted = ID3_KEYS.get(key, key)
        frame = tags.get(wanted)
        if frame is None:
            frame = next((v for k, v in tags.items() if k.startswith(wanted + ":")), None)
        if frame is None:
            return ""
        text = getattr(frame, "text", "")
        if isinstance(text, (list, tuple)):
            text = text[0] if text else ""
        return str(text)
    try:
        v = tags.get(key)
    except Exception:
        return ""
    if not v:
        return ""
    if isinstance(v, (list, tuple)):
        v = v[0]
    if isinstance(v, bytes):
        return v.decode("utf8", "replace")
    return str(v)


def track_from_tags(f, path: Path) -> Track:
    t = Track("")
    t.title = tag_value(f, "TITLE") or path.stem
    raw_artists = tag_value(f, "ARTIST")
    t.artists = [a.strip() for a in re.split(r"[,;]|\s+(?:feat\.?|ft\.?)\s+", raw_artists, flags=re.I) if a.strip()]
    t.album = tag_value(f, "ALBUM")
    t.album_artist = tag_value(f, "ALBUMARTIST")
    t.date = tag_value(f, "DATE")
    t.track_no = int(num) if (num := tag_value(f, "TRACKNUMBER")).isdigit() else 0
    total = tag_value(f, "TRACKTOTAL") or tag_value(f, "TOTALTRACKS")
    t.track_total = int(total) if total.isdigit() else 0
    t.disc_no = int(num) if (num := tag_value(f, "DISCNUMBER")).isdigit() else 0
    t.disc_total = int(num) if (num := tag_value(f, "DISCTOTAL")).isdigit() else 0
    t.duration_ms = round(f.info.length * 1000)
    return t


def update_one(path: Path, yt, job: Job, root: Path | None) -> Result:
    f = mutagen.File(path)
    if f is None:
        raise PipelineError(f"unsupported audio format: {path.suffix}")
    old = track_from_tags(f, path)
    job.title = f"{artist_label(old.artists) or '?'} — {old.title}"
    existing_source = tag_value(f, "SOURCE") or tag_value(f, "COMMENT")

    # 1. Spotify link: SOURCE tag first, then the older COMMENT tag, then search.
    job.set("meta", "active")
    sid = source_id(existing_source)
    via = "SOURCE" if sid else ""
    if not sid and tag_value(f, "COMMENT"):
        sid = source_id(tag_value(f, "COMMENT"))
        via = "COMMENT" if sid else ""
    if not sid:
        try:
            sid = spotify_search_id(old.title, old.artists, old.album)
            via = "Spotify search" if sid else ""
        except Exception:
            sid = ""
    t = old
    if sid:
        # A failed metadata refresh must not lose a perfectly good Spotify id.
        try:
            fresh = spotify_track(sid)
            if similar(old.title, fresh.title) >= 2.0 and artist_ok(old.artists, fresh.artists):
                t = fresh
                t.duration_ms = t.duration_ms or old.duration_ms
                try:  # YT Music only fills what Spotify didn't have
                    hit = find_match(yt, t)
                    if hit:
                        fill_gaps(yt, t, hit)
                except Exception:
                    pass
        except Exception:
            pass
    source_link = f"https://open.spotify.com/track/{sid}" if sid else existing_source
    job.set(
        "meta", "done" if source_link else "skip",
        f"{t.title or old.title}" + (f"  ↳ via {via}" if via else "") if source_link else "no Spotify link",
    )

    # 2. Lyrics: LRCLIB covers most tracks, Genius next, YT Music last.
    job.set("lyrics", "active")
    lyrics = fetch_lyrics(yt, t, round(old.duration_ms / 1000))
    job.set(
        "lyrics", "done" if lyrics else "skip",
        f"{lyrics.source} · {len(lyrics.text)} chars" if lyrics else "none found",
    )

    # 3. Write tags in place. Never move, rename or re-encode the file.
    job.set("tag", "active")
    cover = None
    if t is not old and t.cover_url:
        try:
            cover = fetch_cover(t.cover_url)
        except PipelineError:
            pass
    tag_audio(path, t, cover, lyrics, source_link=source_link)

    notes = []
    source_added = source_refreshed = False
    if not source_link:
        notes.append("no Spotify URL found")
    if source_link and not existing_source:
        notes.append("SOURCE added")
        source_added = True
    elif source_link and source_link != existing_source:
        notes.append("SOURCE refreshed")
        source_refreshed = True
    if lyrics:
        notes.append(f"lyrics: {lyrics.source}")
    else:
        notes.append("no lyrics found (LRCLIB, Genius, YT Music)")
    if cover:
        notes.append("cover refreshed")
    folder = str(path.parent.relative_to(root)) if root and path.parent.is_relative_to(root) else path.parent.name
    job.set("tag", "done", f"{len(notes)} changes written")
    return Result("ok", job.title, folder, ", ".join(notes),
                  no_lyrics=not lyrics, no_source=not source_link,
                  source_added=source_added, source_refreshed=source_refreshed,
                  cover_refreshed=bool(cover), lyrics_source=lyrics.source if lyrics else "",
                  artist=artist_label(t.artists) or artist_label(old.artists) or "Unknown Artist",
                  album=t.album or old.album or "Singles",
                  title=t.title or old.title)


def normalize_music_dir(value: str | None) -> str:
    """`/Music/`, `Music/`, ` Music ` all become `Music`."""
    return (value or "Music").strip().strip("/\\").strip() or "Music"


def resolve_scan_roots(paths: list[str], music_dir: str) -> list[Path]:
    """Only ever scan Music folders (or explicitly named audio files).

    - an audio file is taken as-is
    - a directory named <music-dir> is scanned directly
    - a directory containing <music-dir> scans just that child
    - otherwise only descendant folders named <music-dir> are scanned;
      Audiobooks/, Quran/, Podcasts/ etc. are never swept up by accident.
    """
    roots: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            if p.suffix.lower() in AUDIO_EXTS:
                roots.append(p)
            continue
        if not p.is_dir():
            roots.append(p)  # scan_audio reports it as invalid
            continue
        if p.name.casefold() == music_dir.casefold():
            roots.append(p)
            continue
        child = p / music_dir
        if child.is_dir():
            roots.append(child)
            continue
        nested = sorted({d for d in p.rglob(music_dir) if d.is_dir()})
        if nested:
            roots.extend(nested)
        else:
            console.print(Text(
                f"  ! no '{music_dir}' folder under {p}; skipping "
                "(pass the music folder explicitly)", style=YELLOW,
            ))
    return list(dict.fromkeys(roots))


def default_scan_path(mount_root: str, device: str, music_dir: str) -> str:
    """Where --doctor / --update-metadata look when no path was given."""
    root = Path(mount_root).expanduser()
    dev = resolve_device_path(root, device, music_dir)
    candidates = [dev / music_dir, root / music_dir, root / device, root]
    for c in candidates:
        if not c.is_dir():
            continue
        resolved = resolve_scan_roots([str(c)], music_dir)
        if resolved and all(x.is_dir() for x in resolved):
            return str(resolved[0])
    return str(dev / music_dir)


def scan_audio(paths: list[str], music_dir: str) -> tuple[list[Path], Path | None]:
    roots = resolve_scan_roots(paths, music_dir)
    files: list[Path] = []
    for p in roots:
        if p.is_dir():
            files.extend(sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in AUDIO_EXTS))
        elif p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            files.append(p)
        elif not p.exists():
            console.print(Text(f"  ✗ path not found: {p}", style=RED))
        else:
            console.print(Text(f"  ✗ not an audio file or folder: {p}", style=RED))
    files = list(dict.fromkeys(files))
    return files, (roots[0] if len(roots) == 1 else None)


def run_update(paths: list[str], music_dir: str) -> None:
    from ytmusicapi import YTMusic

    files, root = scan_audio(paths, music_dir)
    if not files:
        console.print(Text("  no supported audio files found to update.", style=RED))
        raise SystemExit(1)
    register_source_seeds(files)

    console.print(
        Panel(
            Group(
                Text(f"{len(files)} audio file{'s' if len(files) != 1 else ''} to refresh", style=SUB),
                Text("metadata: Spotify first (SOURCE → COMMENT → search), YT Music only for gaps", style=DIM),
                Text("lyrics: LRCLIB → Genius → YT Music", style=DIM),
                Text("files are updated in place — nothing is moved or renamed", style=DIM),
            ),
            title=Text(" update metadata ", style=f"bold {BASE} on {MAUVE}"),
            title_align="left",
            border_style=MAUVE,
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )
    yt = YTMusic()
    screen = Screen(None, len(files), UPDATE_STEPS)
    started = time.time()
    interrupted = False
    with Live(get_renderable=screen, console=console, refresh_per_second=12, transient=True):
        try:
            for i, path in enumerate(files, 1):
                job = Job(i, len(files), "", UPDATE_STEPS)
                screen.job = job
                try:
                    res = update_one(path, yt, job, root)
                except PipelineError as e:
                    res = Result("fail", job.title, str(path.parent.name), str(e))
                except Exception as e:
                    res = Result("fail", job.title, str(path.parent.name), f"{type(e).__name__}: {e}")
                if res.status == "fail":
                    for key, (status, _) in job.steps.items():
                        if status == "active":
                            job.set(key, "fail", res.note)
                screen.results.append(res)
                screen.progress.advance(screen.task)
                if not console.is_terminal:
                    mark = {"ok": "ok  ", "skip": "skip", "fail": "FAIL"}[res.status]
                    console.print(f"[{i}/{len(files)}] {mark} {res.label} {res.folder} {res.note}", highlight=False)
        except KeyboardInterrupt:
            interrupted = True
    os.sync()
    console.print(update_summary(screen.results, time.time() - started, interrupted))
    if any(r.status == "fail" for r in screen.results):
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# --doctor: report which required tags/covers are missing, grouped by artist
# --------------------------------------------------------------------------- #

def audio_has_cover(f) -> bool:
    try:
        if isinstance(f, FLAC):
            return bool(f.pictures)
        if isinstance(f, MP4):
            return bool(f.tags and f.tags.get("covr"))
        if isinstance(f, (OggVorbis, OggOpus)):
            return bool(f.tags and f.tags.get("metadata_block_picture"))
        if isinstance(f, ID3):
            tags = f
        else:
            tags = getattr(f, "tags", None)
        if isinstance(tags, ID3):
            return any(k.startswith("APIC") for k in tags.keys())
    except Exception:
        pass
    return False


def doctor_one(path: Path) -> dict:
    f = mutagen.File(path)
    if f is None:
        return {"path": path, "artist": "Unknown Artist", "album": "Singles",
                "source": False, "cover": False, "album_tag": False, "artist_tag": False,
                "unreadable": True}
    source = tag_value(f, "SOURCE") or tag_value(f, "COMMENT")
    artist = tag_value(f, "ARTIST") or "Unknown Artist"
    album = tag_value(f, "ALBUM") or "Singles"
    return {
        "path": path,
        "artist": artist,
        "album": album,
        "source": bool(source_id(source)),
        "cover": audio_has_cover(f),
        "album_tag": bool(tag_value(f, "ALBUM")),
        "artist_tag": bool(tag_value(f, "ARTIST")),
        "unreadable": False,
    }


def doctor_summary(rows: list[dict], elapsed: float) -> Panel:
    def count(key: str) -> int:
        return sum(1 for r in rows if not r[key])

    line = Text()
    line.append(f"✓ {len(rows)} scanned", style=f"bold {TEXT}")
    line.append(f"   · {fmt_time(elapsed)}", style=DIM)
    body: list = [line]
    checks = [
        ("source", "SOURCE link"),
        ("cover", "cover art"),
        ("album_tag", "album"),
        ("artist_tag", "artist"),
    ]
    for key, label in checks:
        missing = count(key)
        if missing:
            body.append(Text.assemble(
                ("! ", YELLOW), (f"{missing}/{len(rows)} missing ", TEXT), (label, DIM),
            ))
    if not any(count(k) for k, _ in checks):
        body.append(Text("all tracks have source, cover, album and artist", style=GREEN))
    return Panel(
        Group(*body),
        title=Text(" doctor summary ", style=f"bold {BASE} on {MAUVE}"),
        title_align="left",
        border_style=MAUVE,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def run_doctor(paths: list[str], music_dir: str) -> None:
    files, _ = scan_audio(paths, music_dir)
    if not files:
        console.print(Text("  no audio files found to check.", style=RED))
        raise SystemExit(1)
    started = time.time()
    rows = [doctor_one(p) for p in files]
    grouped: dict[str, dict[str, list[dict]]] = {}
    for row in rows:
        grouped.setdefault(row["artist"], {}).setdefault(row["album"], []).append(row)
    tree = Tree(Text(f"{len(rows)} songs in {len(grouped)} artists", style=f"bold {TEXT}"))
    for artist, albums in sorted(grouped.items(), key=lambda x: x[0].casefold()):
        a = tree.add(Text(artist, style=f"bold {MAUVE}"))
        for album, songs in sorted(albums.items(), key=lambda x: x[0].casefold()):
            b = a.add(Text(album, style=SUB))
            for row in songs:
                missing = [label for key, label in (
                    ("source", "source"), ("cover", "cover"),
                    ("album_tag", "album"), ("artist_tag", "artist"),
                ) if not row[key]]
                mark = Text("✓", style=GREEN) if not missing else Text("✗", style=RED)
                b.add(Text.assemble(
                    mark, (" " + row["path"].name + "  ", TEXT),
                    (("missing: " + ", ".join(missing)) if missing else "all good", DIM if not missing else YELLOW),
                ))
    console.print(Panel(tree, title=Text(" doctor ", style=f"bold {BASE} on {MAUVE}"),
                        title_align="left", border_style=MAUVE, box=box.ROUNDED, padding=(1, 2)))
    console.print(doctor_summary(rows, time.time() - started))
    if any(not r[k] for r in rows for k in ("source", "cover", "album_tag", "artist_tag")):
        raise SystemExit(1)


def main() -> None:
    p = argparse.ArgumentParser(
        prog="music2dap",
        description=(
            "Music2DAP — Spotify links in, tagged audio in "
            "/run/media/$USER/<device>/Music/<Artist>/<Album>/ out."
        ),
    )
    p.add_argument(
        "urls", nargs="*",
        help="Spotify links, or audio files/folders with --update-metadata",
    )
    p.add_argument(
        "--update-metadata", "--metadata-update", action="store_true", dest="update_metadata",
        help="refresh tags, the Spotify SOURCE link and lyrics in existing audio files/folders",
    )
    p.add_argument(
        "--doctor", action="store_true",
        help="scan audio files and report missing source, cover, album and artist, grouped by artist/album",
    )
    p.add_argument(
        "-f", "--format",
        choices=list(FORMATS),
        default=os.environ.get("MUSIC2DAP_FORMAT", "FLAC").upper(),
        help="output format (default: %(default)s, or $MUSIC2DAP_FORMAT)",
    )
    p.add_argument(
        "-d", "--device",
        default=os.environ.get("MUSIC2DAP_DEVICE", "HiBy R1 SD"),
        help="mounted device name (default: %(default)s, or $MUSIC2DAP_DEVICE)",
    )
    p.add_argument(
        "--mount-root",
        default=os.path.expandvars(os.environ.get("MUSIC2DAP_MOUNT_ROOT") or DEFAULT_MOUNT_ROOT),
        help="where removable media is mounted (default: %(default)s)",
    )
    p.add_argument(
        "--music-dir",
        default=None,
        help="music folder name; bare roots are scanned at <root>/<music-dir> when present "
             "(default: $MUSIC2DAP_MUSIC_DIR or Music)",
    )
    p.add_argument("--version", action="version", version=f"Music2DAP {__version__}")
    args = p.parse_args()
    global ACTIVE_FORMAT
    ACTIVE_FORMAT = FORMATS[args.format]
    music_dir = normalize_music_dir(args.music_dir or os.environ.get("MUSIC2DAP_MUSIC_DIR"))
    scan_targets = args.urls or [default_scan_path(args.mount_root, args.device, music_dir)]

    if args.doctor and args.update_metadata:
        p.error("use either --doctor or --update-metadata, not both")

    if not args.urls and (args.doctor or args.update_metadata) and not Path(scan_targets[0]).exists():
        device_not_mounted(args.mount_root, args.device, scan_targets[0])

    if not args.urls:
        console.print(Text(f"  scanning {scan_targets[0]}", style=DIM))

    if args.doctor:
        run_doctor(scan_targets, music_dir)
        return

    if args.update_metadata:
        run_update(scan_targets, music_dir)
        return

    if not shutil.which("ffmpeg"):
        console.print(Text("ffmpeg not found in PATH", style=RED))
        raise SystemExit(1)

    dev = Device(args.device, Path(args.mount_root), music_dir)
    console.print(banner(dev))
    wait_for_device(dev)

    items = [pr for tok in args.urls if (pr := parse_spotify(tok))] if args.urls else collect_urls()
    if not items:
        console.print(Text("  nothing queued, bye.", style=DIM))
        return
    console.print()
    ids = expand_all(items)
    if not ids:
        console.print(Text("  no tracks to fetch.", style=RED))
        raise SystemExit(1)
    console.print(Text(f"\n  fetching {len(ids)} track{'s' if len(ids) != 1 else ''}…\n", style=SUB))
    run_queue(dev, ids)


if __name__ == "__main__":
    main()
