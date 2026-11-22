import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../models/track.dart';

class SpotifyService {
  SpotifyService({HttpClient? client})
      : _client = client ?? HttpClient(),
        _browser = {
          'User-Agent': 'Mozilla/5.0',
          'Accept-Language': 'en-US,en;q=0.9',
        };

  final HttpClient _client;
  final Map<String, String> _browser;

  static final RegExp _link = RegExp(
    r'open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?(?:embed/)?(track|album|playlist)/([A-Za-z0-9]{22})',
  );
  static final RegExp _uri = RegExp(r'spotify:(track|album|playlist):([A-Za-z0-9]{22})');

  ({String kind, String id})? parse(String input) {
    final m = _link.firstMatch(input) ?? _uri.firstMatch(input);
    if (m == null) return null;
    return (kind: m.group(1)!, id: m.group(2)!);
  }

  Future<String> _get(String url, {Map<String, String>? headers}) async {
    final req = await _client.getUrl(Uri.parse(url));
    for (final e in _browser.entries) {
      req.headers.add(e.key, e.value);
    }
    for (final e in headers?.entries ?? const <MapEntry<String, String>>[]) {
      req.headers.add(e.key, e.value);
    }
    final res = await req.close();
    final body = await utf8.decoder.bind(res).join();
    if (res.statusCode != 200) {
      throw StateError('Spotify returned ${res.statusCode}');
    }
    return body;
  }

  static String unescape(String s) => s
      .replaceAll('&amp;', '&')
      .replaceAll('&quot;', '"')
      .replaceAll('&#39;', "'")
      .replaceAll('&apos;', "'")
      .replaceAll('&lt;', '<')
      .replaceAll('&gt;', '>');

  Map<String, String> _meta(String page) {
    final out = <String, String>{};
    for (final m in RegExp(r'<meta\b[^>]*>', caseSensitive: false).allMatches(page)) {
      final tag = m.group(0)!;
      final k = RegExp(r'\b(?:name|property)="([^"]+)"').firstMatch(tag)?.group(1);
      final v = RegExp(r'\bcontent="([^"]*)"').firstMatch(tag)?.group(1);
      if (k != null && v != null) out.putIfAbsent(k, () => unescape(v));
    }
    return out;
  }

  Map<String, dynamic>? _nextData(String page) {
    final m = RegExp(
      r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
      dotAll: true,
    ).firstMatch(page);
    if (m == null) return null;
    try {
      return jsonDecode(m.group(1)!) as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
  }

  Map<String, dynamic>? _entity(String data) {
    final next = _nextData(data);
    if (next == null) return null;
    try {
      final state = (((next['props'] as Map)['pageProps'] as Map)['state'] as Map)['data'] as Map;
      return state['entity'] as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
  }

  Future<TrackInfo> track(String id) async {
    final page = await _get('https://open.spotify.com/track/$id');
    final meta = _meta(page);
    final t = TrackInfo(
      id: id,
      title: meta['og:title'] ?? '',
      coverUrl: meta['og:image'] ?? '',
      date: meta['music:release_date'] ?? '',
    );
    if (RegExp(r'^\d+$').hasMatch(meta['music:duration'] ?? '')) {
      t.durationSeconds = int.parse(meta['music:duration']!);
    }
    if (RegExp(r'^\d+$').hasMatch(meta['music:album:track'] ?? '')) {
      t.trackNo = int.parse(meta['music:album:track']!);
    }
    final albumId = RegExp(r'/album/([A-Za-z0-9]{22})')
        .firstMatch(meta['music:album'] ?? '')?.group(1);

    final embed = await _get('https://open.spotify.com/embed/track/$id');
    final ent = _entity(embed);
    if (ent != null) {
      t.artists = (ent['artists'] as List? ?? [])
          .map((a) => (a as Map)['name']?.toString() ?? '')
          .where((s) => s.isNotEmpty)
          .toList();
      if (t.title.isEmpty) t.title = ent['title']?.toString() ?? ent['name']?.toString() ?? '';
      if (t.durationSeconds == 0) t.durationSeconds = int.tryParse('${ent['duration'] ?? 0}') ?? 0;
    }
    if (t.artists.isEmpty && meta.containsKey('music:musician_description')) {
      t.artists = [meta['music:musician_description']!];
    }
    if (t.title.isEmpty || t.artists.isEmpty) {
      throw StateError('Could not read title/artist from Spotify (removed or region-locked?)');
    }

    if (albumId != null) {
      final album = await _album(albumId);
      t.album = album.$1;
      t.albumArtist = album.$2;
      if (t.coverUrl.isEmpty) t.coverUrl = album.$3;
      if (t.date.isEmpty) t.date = album.$4;
      for (final row in album.$5) {
        if (row.$1 == id) {
          t.discNo = row.$2;
          t.trackNo = row.$3;
        }
      }
      if (t.discNo > 0) {
        t.trackTotal = album.$5.where((r) => r.$2 == t.discNo).length;
        t.discTotal = album.$5.map((r) => r.$2).fold<int>(0, (a, b) => a > b ? a : b);
      }
    }
    return t;
  }

  Future<(String, String, String, String, List<(String, int, int)>)> _album(String id) async {
    final page = await _get('https://open.spotify.com/album/$id');
    final meta = _meta(page);
    final songs = <(String, int, int)>[];
    for (final e in meta.entries) {
      if (e.key == 'music:song') {
        final m = RegExp(r'/track/([A-Za-z0-9]{22})').firstMatch(e.value);
        songs.add((m?.group(1) ?? '', 1, songs.length + 1));
      }
    }
    var name = '';
    var artist = '';
    final m = RegExp(r'Listen to (.*) - Album by .* \| Spotify').firstMatch(meta['og:title'] ?? '');
    if (m != null) name = m.group(1)!;
    final embed = await _get('https://open.spotify.com/embed/album/$id');
    final ent = _entity(embed);
    if (ent != null) {
      if (name.isEmpty) name = ent['name']?.toString() ?? ent['title']?.toString() ?? '';
      artist = ent['subtitle']?.toString() ?? '';
      if (songs.isEmpty) {
        var n = 1;
        for (final item in (ent['trackList'] as List? ?? [])) {
          final uri = (item as Map)['uri']?.toString() ?? '';
          final tm = RegExp(r'spotify:track:([A-Za-z0-9]{22})').firstMatch(uri);
          if (tm != null) songs.add((tm.group(1)!, 1, n++));
        }
      }
    }
    return (name, artist, meta['og:image'] ?? '', meta['music:release_date'] ?? '', songs);
  }

  Future<List<TrackInfo>> expand(String input) async {
    final parsed = parse(input);
    if (parsed == null) throw ArgumentError('Not a Spotify link: $input');
    if (parsed.kind == 'track') return [await track(parsed.id)];
    final page = await _get('https://open.spotify.com/embed/${parsed.kind}/${parsed.id}');
    final ent = _entity(page);
    final ids = <String>[];
    for (final item in (ent?['trackList'] as List? ?? [])) {
      final m = RegExp(r'spotify:track:([A-Za-z0-9]{22})')
          .firstMatch((item as Map)['uri']?.toString() ?? '');
      if (m != null) ids.add(m.group(1)!);
    }
    final out = <TrackInfo>[];
    for (final id in ids.toSet()) {
      out.add(await track(id));
    }
    return out;
  }
}
