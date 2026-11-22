import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../models/track.dart';

class Lyrics {
  const Lyrics(this.text, this.source);

  final String text;
  final String source;

  bool get found => text.trim().isNotEmpty;
}

String _norm(String s) => s
    .toLowerCase()
    .replaceAll(RegExp(r'[^\p{L}\p{N}]+', unicode: true), ' ')
    .trim();

bool _artistOk(List<String> wanted, String got) {
  final blob = _norm(got);
  return wanted.any(
    (a) => _norm(a).split(' ').any((w) => w.length > 1 && blob.contains(w)),
  );
}

bool _titleOk(String wanted, String got) {
  final a = _norm(wanted), b = _norm(got);
  if (a.isEmpty || b.isEmpty) return false;
  if (a == b) return true;
  final aw = a.split(' ').where((w) => w.length > 2).toSet();
  final bw = b.split(' ').where((w) => w.length > 2).toSet();
  if (aw.isEmpty || bw.isEmpty) return false;
  final common = aw.intersection(bw).length;
  return common >= aw.length * 0.8 && common >= bw.length * 0.6;
}

class LyricsService {
  LyricsService({HttpClient? client})
    : _client = client ?? HttpClient(),
      _headers = {
        'User-Agent':
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
      };

  final HttpClient _client;
  final Map<String, String> _headers;

  Future<String> _get(Uri uri) async {
    final req = await _client.getUrl(uri);
    for (final e in _headers.entries) {
      req.headers.add(e.key, e.value);
    }
    final res = await req.close();
    if (res.statusCode != 200) return '';
    return utf8.decoder.bind(res).join();
  }

  static String _strip(String raw) {
    var s = raw
        .replaceAll('\r\n', '\n')
        .replaceAll('\r', '\n')
        .replaceAll(RegExp(r'<br\s*/?>', caseSensitive: false), '\n')
        .replaceAll(RegExp(r'<[^>]+>'), '');
    const junk = [
      'how to format lyrics',
      'you might also like',
      'read more',
      'learn more',
      'sign up',
      'song bio',
    ];
    final lines = <String>[];
    for (final line in s.split('\n')) {
      final t = line.trim();
      if (t.isEmpty || t.toLowerCase() == 'lyrics') continue;
      if (junk.any((j) => t.toLowerCase().contains(j))) continue;
      lines.add(t);
    }
    s = lines.join('\n');
    final clean = <String>[];
    for (final line in s.split('\n')) {
      if (line.isEmpty && clean.isNotEmpty && clean.last.isEmpty) continue;
      clean.add(line);
    }
    return clean.join('\n').trim();
  }

  Future<Lyrics?> _lrclib(TrackInfo t) async {
    final q = <String, String>{
      'track_name': t.title,
      if (t.artists.isNotEmpty) 'artist_name': t.artists.first,
      if (t.album.isNotEmpty) 'album_name': t.album,
      if (t.durationSeconds > 0) 'duration': '${t.durationSeconds}',
    };
    final body = await _get(Uri.https('lrclib.net', '/api/get', q));
    if (body.isEmpty) return null;
    try {
      final row = jsonDecode(body) as Map<String, dynamic>;
      // /api/get returns a best match, not an exact one. Reject rows whose
      // artist/title disagree with Spotify instead of saving wrong lyrics.
      if (!_artistOk(t.artists, (row['artistName'] ?? '').toString()) ||
          !_titleOk(t.title, (row['trackName'] ?? '').toString())) {
        return null;
      }
      final text = _strip(
        (row['plainLyrics'] ?? row['syncedLyrics'] ?? '').toString(),
      );
      if (text.length >= 20) return Lyrics(text, 'LRCLIB');
    } catch (_) {}
    return null;
  }

  Future<Lyrics?> _genius(TrackInfo t) async {
    final query = '${t.artists.isEmpty ? '' : '${t.artists.first} '}${t.title}';
    final body = await _get(
      Uri.https('genius.com', '/api/search/multi', {'q': query}),
    );
    if (body.isEmpty) return null;
    String? path;
    try {
      final decoded = jsonDecode(body) as Map<String, dynamic>;
      final sections = (((decoded['response'] as Map)['sections']) as List);
      for (final sec in sections) {
        for (final hit in ((sec as Map)['hits'] as List? ?? [])) {
          final row = (hit as Map)['result'] as Map?;
          if (row == null || hit['type'] != 'song') continue;
          if (row['instrumental'] == true) continue;
          // Never take a same-titled song by a different artist.
          if (!_artistOk(t.artists, (row['artist_names'] ?? '').toString())) {
            continue;
          }
          final title = row['title']?.toString() ?? '';
          if (title.toLowerCase().contains(t.title.toLowerCase()) ||
              t.title.toLowerCase().contains(title.toLowerCase())) {
            path = row['path']?.toString();
            break;
          }
        }
        if (path != null) break;
      }
    } catch (_) {}
    if (path == null) return null;
    final page = await _get(Uri.https('genius.com', path));
    if (page.isEmpty) return null;
    final parts = RegExp(
      r'<div[^>]*data-lyrics-container="true"[^>]*>(.*?)</div>',
      dotAll: true,
    ).allMatches(page).map((m) => m.group(1) ?? '').join('\n');
    final text = _strip(parts);
    return text.length >= 20 ? Lyrics(text, 'Genius') : null;
  }

  /// The same fixed order as the CLI: LRCLIB, Genius, YT Music.
  /// YT Music lyrics are fetched by the CLI when available; the GUI reports
  /// plainly when none of the sources had anything.
  Future<Lyrics?> fetch(TrackInfo t) async {
    final first = await _lrclib(t);
    if (first != null) return first;
    return _genius(t);
  }
}
