import 'dart:convert';
import 'dart:io';

import '../models/track.dart';
import 'lyrics.dart';
import 'settings.dart';

class AudioFormatInfo {
  const AudioFormatInfo(
    this.name,
    this.ext,
    this.codec,
    this.args, {
    this.coverCodec = 'copy',
    this.id3v2 = false,
  });

  final String name;
  final String ext;
  final String codec;
  final List<String> args;
  final String coverCodec;
  final bool id3v2;
}

const audioFormats = <String, AudioFormatInfo>{
  'FLAC': AudioFormatInfo('FLAC', 'flac', 'flac', ['-compression_level', '8']),
  'MP3': AudioFormatInfo(
    'MP3',
    'mp3',
    'libmp3lame',
    ['-q:a', '2'],
    coverCodec: 'mjpeg',
    id3v2: true,
  ),
  'OPUS': AudioFormatInfo('OPUS', 'opus', 'libopus', ['-b:a', '128k']),
};

String sanitize(String s) => s
    .replaceAll(RegExp(r'[\\/:*?"<>|]'), '_')
    .replaceAll(RegExp(r'\s+'), ' ')
    .trim();

String targetDir(AppSettings settings, TrackInfo t) {
  final artist = sanitize(t.artists.isEmpty ? 'Unknown Artist' : t.artists.first);
  final album = (t.album.isEmpty || t.trackTotal <= 1) ? 'Singles' : sanitize(t.album);
  return '${settings.libraryPath}$Platform.pathSeparator$artist$Platform.pathSeparator$album';
}

String targetName(TrackInfo t, AudioFormatInfo f) {
  final base = t.trackNo > 0
      ? '${t.trackNo.toString().padLeft(2, '0')} - ${sanitize(t.title)}'
      : sanitize(t.title);
  return '$base.${f.ext}';
}

class DownloadResult {
  DownloadResult({required this.ok, this.path = '', this.message = '', this.lyricsSource = ''});
  final bool ok;
  final String path;
  final String message;
  final String lyricsSource;
}

class Downloader {
  Downloader(this.settings, this._log);
  final AppSettings settings;
  final void Function(String) _log;

  Future<File?> _cover(TrackInfo t, Directory temp) async {
    if (t.coverUrl.isEmpty) return null;
    try {
      final uri = Uri.parse(t.coverUrl);
      final req = await HttpClient().getUrl(uri);
      req.headers.add('User-Agent', 'Mozilla/5.0');
      final res = await req.close();
      if (res.statusCode != 200) return null;
      final bytes = await res.fold<List<int>>(<int>[], (a, b) => a..addAll(b));
      final f = File('${temp.path}${Platform.pathSeparator}cover.jpg');
      await f.writeAsBytes(bytes);
      return f;
    } catch (e) {
      _log('cover skipped: $e');
      return null;
    }
  }

  Future<List<String>> _run(String exe, List<String> args) async {
    final r = await Process.run(exe, args);
    final out = '${r.stdout}\n${r.stderr}'.trim();
    if (out.isNotEmpty) {
      for (final line in out.split('\n')) {
        if (line.trim().isNotEmpty) _log(line.trim());
      }
    }
    if (r.exitCode != 0) throw StateError('$exe exited ${r.exitCode}');
    return out.split('\n');
  }

  Future<DownloadResult> download(TrackInfo t, {LyricsService? lyricsService}) async {
    final fmt = audioFormats[settings.format] ?? audioFormats['FLAC']!;
    final temp = await Directory.systemTemp.createTemp('m2d_');
    try {
      _log('searching YouTube Music for: ${t.searchQuery}');
      final isFlac = fmt.name == 'FLAC';
      final ytdlpArgs = <String>[
        '-f', isFlac ? 'bestaudio[ext=flac]/bestaudio' : 'bestaudio',
        '--no-playlist',
        '--no-warnings',
        '--retries', '5',
        '--fragment-retries', '5',
      ];
      if (isFlac) {
        // Audio-only FLAC at max compression; yt-dlp prints the final path.
        ytdlpArgs.addAll([
          '-x', '--audio-format', 'flac',
          '--postprocessor-args', 'ffmpeg:-vn -compression_level 8',
          '--print', 'after_move:filepath',
          '-o', '${temp.path}${Platform.pathSeparator}track.%(ext)s',
        ]);
      } else {
        ytdlpArgs.addAll(['-o', '${temp.path}${Platform.pathSeparator}raw.%(ext)s']);
      }
      ytdlpArgs.add('ytmsearch1:${t.searchQuery}');
      final ytdlpOut = await _run(settings.ytdlp, ytdlpArgs);

      late final File raw;
      if (isFlac) {
        final printed = ytdlpOut.map((s) => s.trim()).where((s) => s.isNotEmpty);
        final path = printed.lastWhere(
          (s) => s.toLowerCase().endsWith('.flac') && File(s).existsSync(),
          orElse: () => '',
        );
        raw = path.isNotEmpty
            ? File(path)
            : temp.listSync().whereType<File>().firstWhere(
                  (f) => f.path.toLowerCase().endsWith('.flac'),
                  orElse: () => throw StateError('yt-dlp did not produce a FLAC file'),
                );
      } else {
        raw = temp.listSync().whereType<File>().firstWhere(
              (f) => f.path.split(Platform.pathSeparator).last.startsWith('raw.'),
              orElse: () => throw StateError('yt-dlp did not produce an audio file'),
            );
      }

      Lyrics? lyrics;
      String source = '';
      try {
        lyrics = await (lyricsService ?? LyricsService()).fetch(t);
        source = lyrics?.source ?? '';
      } catch (e) {
        _log('lyrics lookup failed: $e');
      }
      if (lyrics == null || !lyrics.found) {
        _log('no lyrics found for ${t.display}');
      }

      final cover = await _cover(t, temp);
      final dir = Directory(targetDir(settings, t));
      await dir.create(recursive: true);
      final out = '${dir.path}${Platform.pathSeparator}${targetName(t, fmt)}';

      final meta = t.metadata;
      meta['lyrics'] = lyrics?.text ?? '';

      List<String> ffmpegArgs({required bool withCover}) {
        final a = ['-y', '-i', raw.path];
        if (withCover && cover != null) a.addAll(['-i', cover.path]);
        a.addAll(['-map', '0:a:0']);
        if (withCover && cover != null) a.addAll(['-map', '1:v:0']);
        a.addAll(['-c:a', fmt.codec, ...fmt.args]);
        if (withCover && cover != null) a.addAll(['-c:v', fmt.coverCodec]);
        for (final e in meta.entries) {
          if (e.value.isNotEmpty) a.addAll(['-metadata', '${e.key}=${e.value}']);
        }
        if (fmt.id3v2) a.addAll(['-id3v2_version', '3', '-write_id3v1', '1']);
        a.add(out);
        return a;
      }

      try {
        await _run(settings.ffmpeg, ffmpegArgs(withCover: cover != null));
      } catch (e) {
        if (cover == null) rethrow;
        _log('cover embed failed, retrying without art: $e');
        await _run(settings.ffmpeg, ffmpegArgs(withCover: false));
      }

      return DownloadResult(
        ok: true,
        path: out,
        message: '${t.display} → ${targetName(t, fmt)}',
        lyricsSource: source,
      );
    } catch (e) {
      return DownloadResult(ok: false, message: '${t.display}: $e');
    } finally {
      try {
        await temp.delete(recursive: true);
      } catch (_) {}
    }
  }
}

// ---------------------------------------------------------------------------
// Doctor: read tags with ffprobe, group by artist / album
// ---------------------------------------------------------------------------

class DoctorRow {
  DoctorRow({
    required this.path,
    required this.artist,
    required this.album,
    required this.source,
    required this.cover,
    required this.albumTag,
    required this.artistTag,
  });

  final String path;
  final String artist;
  final String album;
  final bool source;
  final bool cover;
  final bool albumTag;
  final bool artistTag;

  List<String> get missing {
    final m = <String>[];
    if (!source) m.add('source');
    if (!cover) m.add('cover');
    if (!albumTag) m.add('album');
    if (!artistTag) m.add('artist');
    return m;
  }

  bool get ok => missing.isEmpty;
}

String ffprobeFor(String ffmpeg) {
  var idx = ffmpeg.lastIndexOf(RegExp(r'[\\/]'));
  final dir = idx >= 0 ? ffmpeg.substring(0, idx + 1) : '';
  var base = ffmpeg.substring(idx + 1);
  if (base.toLowerCase().endsWith('.exe')) base = base.substring(0, base.length - 4);
  base = base.replaceAll('ffmpeg', 'ffprobe');
  return '$dir$base${Platform.isWindows ? '.exe' : ''}';
}

const _audioExts = {'.flac', '.mp3', '.opus', '.m4a', '.ogg', '.wav', '.aac', '.aiff', '.wma'};

/// Same rule as the CLI: only Music folders (or an explicit audio file) are
/// ever scanned. Audiobooks/, Quran/, Podcasts/ next to Music/ are ignored.
List<Directory> _musicRoots(String raw, String musicDir, void Function(String) log) {
  final p = Directory(raw);
  if (p.existsSync() && FileSystemEntity.isFileSync(raw)) return [];
  if (!p.existsSync()) {
    log('directory does not exist: $raw');
    return [];
  }
  if (p.path.split(RegExp(r'[\\/]')).last.toLowerCase() == musicDir.toLowerCase()) {
    return [p];
  }
  final child = Directory('${p.path}${Platform.pathSeparator}$musicDir');
  if (child.existsSync()) return [child];
  final nested = p
      .listSync(recursive: true)
      .whereType<Directory>()
      .where((d) => d.path.split(RegExp(r'[\\/]')).last.toLowerCase() == musicDir.toLowerCase())
      .toList();
  if (nested.isEmpty) {
    log('no "$musicDir" folder under $raw; nothing scanned');
  }
  return nested;
}

Future<List<DoctorRow>> scanLibrary(
  String root,
  String ffmpeg,
  void Function(String) log, {
  String musicDir = 'Music',
}) async {
  final roots = _musicRoots(root, musicDir, log);
  final files = <File>[];
  for (final dir in roots) {
    files.addAll(dir.listSync(recursive: true).whereType<File>().where((f) => _audioExts.contains(_ext(f.path))));
  }
  files.sort((a, b) => a.path.compareTo(b.path));
  final probe = ffprobeFor(ffmpeg);
  final rows = <DoctorRow>[];
  for (final f in files) {
    try {
      final r = await Process.run(probe, [
        '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', f.path,
      ]);
      if (r.exitCode != 0) throw StateError('exit ${r.exitCode}');
      final data = jsonDecode(r.stdout as String) as Map<String, dynamic>;
      final tags = <String, String>{};
      final rawTags = (data['format']?['tags'] as Map?) ?? {};
      rawTags.forEach((k, v) => tags[k.toString().toLowerCase()] = v.toString());
      final streams = (data['streams'] as List?) ?? [];
      final hasCover = streams.any((s) {
        final m = s as Map;
        final dispo = (m['disposition'] as Map?) ?? {};
        return dispo['attached_pic'] == 1 ||
            m['codec_type'] == 'attachment' ||
            ((m['tags'] as Map?) ?? {}).containsKey('metadata_block_picture');
      });
      final source = tags['source'] ?? tags['comment'] ?? '';
      final artist = tags['artist'] ?? '';
      final album = tags['album'] ?? '';
      rows.add(DoctorRow(
        path: f.path,
        artist: artist.isEmpty ? 'Unknown Artist' : artist,
        album: album.isEmpty ? 'Singles' : album,
        source: source.trim().isNotEmpty,
        cover: hasCover,
        albumTag: album.trim().isNotEmpty,
        artistTag: artist.trim().isNotEmpty,
      ));
    } catch (e) {
      log('could not read ${f.path}: $e');
      rows.add(DoctorRow(
        path: f.path,
        artist: 'Unknown Artist',
        album: 'Singles',
        source: false,
        cover: false,
        albumTag: false,
        artistTag: false,
      ));
    }
  }
  return rows;
}

String _ext(String path) {
  final i = path.lastIndexOf('.');
  return i < 0 ? '' : path.substring(i).toLowerCase();
}
