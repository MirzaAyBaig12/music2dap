import 'dart:io';

import 'package:flutter/material.dart';

import 'models/track.dart';
import 'services/downloader.dart';
import 'services/lyrics.dart';
import 'services/settings.dart';
import 'services/spotify.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final settings = await AppSettings.load();
  runApp(Music2DapApp(settings: settings));
}

class Music2DapApp extends StatelessWidget {
  const Music2DapApp({super.key, required this.settings});

  final AppSettings settings;

  @override
  Widget build(BuildContext context) {
    final seed = const Color(0xFFB07BD8);
    return MaterialApp(
      title: 'Music2DAP',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: seed, brightness: Brightness.dark),
        scaffoldBackgroundColor: const Color(0xFF111017),
        cardTheme: CardThemeData(
          color: const Color(0xFF1B1924),
          elevation: 0,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        ),
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: const Color(0xFF211E2C),
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(10), borderSide: BorderSide.none),
          contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        ),
        filledButtonTheme: FilledButtonThemeData(
          style: FilledButton.styleFrom(
            backgroundColor: seed,
            foregroundColor: const Color(0xFF120E19),
            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
          ),
        ),
      ),
      home: AppShell(settings: settings),
    );
  }
}

class AppShell extends StatefulWidget {
  const AppShell({super.key, required this.settings});
  final AppSettings settings;

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Row(
        children: [
          NavigationRail(
            extended: true,
            minExtendedWidth: 190,
            backgroundColor: const Color(0xFF17151F),
            selectedIndex: _index,
            onDestinationSelected: (i) => setState(() => _index = i),
            leading: Padding(
              padding: const EdgeInsets.fromLTRB(8, 20, 8, 24),
              child: Row(
                children: [
                  Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: const Color(0xFFB07BD8).withValues(alpha: .18),
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: const Icon(Icons.graphic_eq, color: Color(0xFFD6B8F0)),
                  ),
                  const SizedBox(width: 10),
                  const Text(
                    'Music2DAP',
                    style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, letterSpacing: .3),
                  ),
                ],
              ),
            ),
            destinations: const [
              NavigationRailDestination(icon: Icon(Icons.download_for_offline_outlined), label: Text('Download')),
              NavigationRailDestination(icon: Icon(Icons.health_and_safety_outlined), label: Text('Doctor')),
              NavigationRailDestination(icon: Icon(Icons.tune), label: Text('Settings')),
            ],
          ),
          const VerticalDivider(width: 1, thickness: 1),
          Expanded(
            child: IndexedStack(
              index: _index,
              children: [
                DownloadPage(settings: widget.settings),
                DoctorPage(settings: widget.settings),
                SettingsPage(settings: widget.settings),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Download page
// ---------------------------------------------------------------------------

class DownloadPage extends StatefulWidget {
  const DownloadPage({super.key, required this.settings});
  final AppSettings settings;

  @override
  State<DownloadPage> createState() => _DownloadPageState();
}

class _DownloadPageState extends State<DownloadPage> {
  final _links = TextEditingController();
  final _log = ValueNotifier<List<String>>([]);
  final _tracks = ValueNotifier<List<TrackInfo>>([]);
  final _summary = ValueNotifier<String>('');
  bool _busy = false;
  String _stage = '';

  void log(String s) {
    final now = DateTime.now();
    final stamp = '${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}:${now.second.toString().padLeft(2, '0')}';
    _log.value = [..._log.value, '$stamp  $s'];
  }

  Future<void> preview() async {
    final lines = _links.text.split('\n').map((s) => s.trim()).where((s) => s.isNotEmpty).toList();
    if (lines.isEmpty) {
      _summary.value = 'Paste at least one Spotify link.';
      return;
    }
    setState(() => _busy = true);
    _stage = 'Fetching metadata from Spotify…';
    _log.value = [];
    _summary.value = '';
    _tracks.value = [];
    final out = <TrackInfo>[];
    final service = SpotifyService();
    for (final line in lines) {
      try {
        final found = await service.expand(line);
        out.addAll(found);
        log('${found.length} track(s) from $line');
      } catch (e) {
        log('could not read $line: $e');
      }
    }
    _tracks.value = out;
    _summary.value = out.isEmpty
        ? 'No tracks resolved. Check the links.'
        : '${out.length} track(s) ready. Lyrics: LRCLIB → Genius → YT Music.';
    setState(() => _busy = false);
  }

  Future<void> downloadAll() async {
    final tracks = _tracks.value;
    if (tracks.isEmpty) {
      _summary.value = 'Preview some links first.';
      return;
    }
    setState(() => _busy = true);
    final downloader = Downloader(widget.settings, log);
    final lyrics = LyricsService();
    var ok = 0;
    var failed = 0;
    var withLyrics = 0;
    final written = <String>[];
    for (var i = 0; i < tracks.length; i++) {
      _stage = 'Downloading ${i + 1}/${tracks.length}: ${tracks[i].display}';
      final r = await downloader.download(tracks[i], lyricsService: lyrics);
      if (r.ok) {
        ok++;
        if (r.lyricsSource.isNotEmpty) withLyrics++;
        written.add(r.path);
        log('✓ ${r.message}');
      } else {
        failed++;
        log('✗ ${r.message}');
      }
    }
    _summary.value = 'Done: $ok downloaded, $failed failed, $withLyrics with lyrics.\n'
        '${written.join('\n')}\n\nRescan your DAP library if the songs do not appear.';
    setState(() => _busy = false);
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.all(28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Download', style: Theme.of(context).textTheme.headlineMedium?.copyWith(fontWeight: FontWeight.w800)),
          const SizedBox(height: 4),
          Text(
            'Spotify links → tagged ${widget.settings.format} in ${widget.settings.libraryPath}',
            style: TextStyle(color: cs.onSurface.withValues(alpha: .6)),
          ),
          const SizedBox(height: 20),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(18),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  TextField(
                    controller: _links,
                    maxLines: 5,
                    decoration: const InputDecoration(
                      hintText: 'Paste Spotify track, album or playlist links — one per line',
                      alignLabelWithHint: true,
                    ),
                  ),
                  const SizedBox(height: 14),
                  Row(
                    children: [
                      FilledButton.icon(
                        onPressed: _busy ? null : preview,
                        icon: const Icon(Icons.search),
                        label: const Text('Preview tracks'),
                      ),
                      const SizedBox(width: 12),
                      FilledButton.icon(
                        onPressed: _busy ? null : downloadAll,
                        icon: const Icon(Icons.download),
                        label: const Text('Download all'),
                      ),
                      const Spacer(),
                      Text('format: ${widget.settings.format}', style: TextStyle(color: cs.onSurface.withValues(alpha: .55))),
                    ],
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          if (_busy) LinearProgressIndicator(minHeight: 3, value: null),
          if (_busy) Text(_stage, style: TextStyle(color: cs.primary)),
          const SizedBox(height: 10),
          Expanded(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Expanded(
                  child: ValueListenableBuilder<List<TrackInfo>>(
                    valueListenable: _tracks,
                    builder: (_, tracks, _) => Card(
                      child: tracks.isEmpty
                          ? const Center(child: Text('No tracks previewed yet.'))
                          : ListView.separated(
                              padding: const EdgeInsets.all(8),
                              itemCount: tracks.length,
                              separatorBuilder: (_, _) => const Divider(height: 1),
                              itemBuilder: (_, i) {
                                final t = tracks[i];
                                return ListTile(
                                  leading: t.coverUrl.isEmpty
                                      ? const Icon(Icons.music_note)
                                      : Image.network(t.coverUrl, width: 46, height: 46, fit: BoxFit.cover,
                                          errorBuilder: (_, _, _) => const Icon(Icons.music_note)),
                                  title: Text(t.title, overflow: TextOverflow.ellipsis),
                                  subtitle: Text('${t.artists.join(', ')}${t.album.isEmpty ? '' : ' · ${t.album}'}',
                                      overflow: TextOverflow.ellipsis),
                                  trailing: t.trackNo > 0 ? Text('${t.trackNo}'.padLeft(2, '0')) : null,
                                );
                              },
                            ),
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Card(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Padding(
                          padding: const EdgeInsets.fromLTRB(16, 14, 16, 8),
                          child: Text('Activity', style: TextStyle(color: cs.primary, fontWeight: FontWeight.bold)),
                        ),
                        Expanded(
                          child: ValueListenableBuilder<List<String>>(
                            valueListenable: _log,
                            builder: (_, lines, _) => ListView(
                              padding: const EdgeInsets.symmetric(horizontal: 16),
                              children: lines.map((l) => Padding(
                                padding: const EdgeInsets.symmetric(vertical: 2),
                                child: Text(l, style: const TextStyle(fontFamily: 'monospace', fontSize: 11.5)),
                              )).toList(),
                            ),
                          ),
                        ),
                        ValueListenableBuilder<String>(
                          valueListenable: _summary,
                          builder: (_, text, _) => text.isEmpty
                              ? const SizedBox.shrink()
                              : Container(
                                  margin: const EdgeInsets.all(12),
                                  padding: const EdgeInsets.all(12),
                                  decoration: BoxDecoration(
                                    color: cs.primary.withValues(alpha: .1),
                                    borderRadius: BorderRadius.circular(10),
                                  ),
                                  child: Text(text, style: const TextStyle(fontSize: 12.5)),
                                ),
                        ),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Doctor page
// ---------------------------------------------------------------------------

class DoctorPage extends StatefulWidget {
  const DoctorPage({super.key, required this.settings});
  final AppSettings settings;

  @override
  State<DoctorPage> createState() => _DoctorPageState();
}

class _DoctorPageState extends State<DoctorPage> {
  late final TextEditingController _root;
  final _rows = ValueNotifier<List<DoctorRow>>([]);
  final _log = ValueNotifier<List<String>>([]);
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _root = TextEditingController(text: widget.settings.libraryPath);
  }

  Future<void> scan() async {
    setState(() => _busy = true);
    _log.value = [];
    _rows.value = [];
    final rows = await scanLibrary(
      _root.text.trim(),
      widget.settings.ffmpeg,
      (s) => _log.value = [..._log.value, s],
      musicDir: widget.settings.musicDir,
    );
    _rows.value = rows;
    setState(() => _busy = false);
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.all(28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Doctor', style: Theme.of(context).textTheme.headlineMedium?.copyWith(fontWeight: FontWeight.w800)),
          const SizedBox(height: 4),
          Text('Checks every track for a source link, cover, album and artist — grouped by artist and album.',
              style: TextStyle(color: cs.onSurface.withValues(alpha: .6))),
          const SizedBox(height: 20),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Row(
                children: [
                  Expanded(child: TextField(controller: _root, decoration: const InputDecoration(labelText: 'Music directory'))),
                  const SizedBox(width: 12),
                  FilledButton.icon(onPressed: _busy ? null : scan, icon: const Icon(Icons.fact_check), label: const Text('Scan')),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
          if (_busy) const LinearProgressIndicator(minHeight: 3),
          Expanded(
            child: ValueListenableBuilder<List<DoctorRow>>(
              valueListenable: _rows,
              builder: (_, rows, _) {
                if (rows.isEmpty) {
                  return const Card(child: Center(child: Text('No tracks scanned yet.')));
                }
                final byArtist = <String, Map<String, List<DoctorRow>>>{};
                for (final r in rows) {
                  byArtist.putIfAbsent(r.artist, () => {}).putIfAbsent(r.album, () => []).add(r);
                }
                final ok = rows.where((r) => r.ok).length;
                return Column(
                  children: [
                    Card(
                      child: Padding(
                        padding: const EdgeInsets.all(14),
                        child: Row(
                          children: [
                            Icon(Icons.summarize, color: cs.primary),
                            const SizedBox(width: 10),
                            Text('${rows.length} scanned · $ok complete · ${rows.length - ok} with missing fields',
                                style: const TextStyle(fontWeight: FontWeight.w600)),
                          ],
                        ),
                      ),
                    ),
                    Expanded(
                      child: ListView(
                        children: byArtist.entries.map((ae) {
                          return ExpansionTile(
                            leading: const Icon(Icons.person_outline),
                            title: Text(ae.key, style: const TextStyle(fontWeight: FontWeight.bold)),
                            subtitle: Text('${ae.value.values.fold<int>(0, (a, b) => a + b.length)} song(s)'),
                            children: ae.value.entries.map((al) {
                              return ExpansionTile(
                                title: Text(al.key),
                                initiallyExpanded: true,
                                children: al.value.map((r) => ListTile(
                                      leading: Icon(r.ok ? Icons.check_circle : Icons.error_outline,
                                          color: r.ok ? Colors.greenAccent : Colors.orangeAccent),
                                      title: Text(r.path.split(Platform.pathSeparator).last),
                                      subtitle: Text(r.ok ? 'all good' : 'missing: ${r.missing.join(', ')}'),
                                    )).toList(),
                              );
                            }).toList(),
                          );
                        }).toList(),
                      ),
                    ),
                  ],
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Settings page
// ---------------------------------------------------------------------------

class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key, required this.settings});
  final AppSettings settings;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final TextEditingController _mount, _device, _musicDir, _ytdlp, _ffmpeg;
  late String _format;
  String _saved = '';

  @override
  void initState() {
    super.initState();
    final s = widget.settings;
    _mount = TextEditingController(text: s.mountRoot);
    _device = TextEditingController(text: s.deviceMount);
    _musicDir = TextEditingController(text: s.musicDir);
    _ytdlp = TextEditingController(text: s.ytdlp);
    _ffmpeg = TextEditingController(text: s.ffmpeg);
    _format = s.format;
  }

  Future<void> save() async {
    final s = widget.settings;
    s
      ..mountRoot = _mount.text.trim()
      ..deviceMount = _device.text.trim()
      ..musicDir = _musicDir.text.trim()
      ..ytdlp = _ytdlp.text.trim()
      ..ffmpeg = _ffmpeg.text.trim()
      ..format = _format;
    await s.save();
    setState(() => _saved = 'Saved. Target: ${s.libraryPath}');
  }

  Widget field(String label, TextEditingController c, {String? hint}) => Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: TextField(controller: c, decoration: InputDecoration(labelText: label, hintText: hint)),
      );

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(28),
      child: SingleChildScrollView(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Settings', style: Theme.of(context).textTheme.headlineMedium?.copyWith(fontWeight: FontWeight.w800)),
            const SizedBox(height: 20),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    field('Mount root (\$USER is expanded)', _mount, hint: '/run/media/\$USER'),
                    field('Device mount name', _device, hint: 'HiBy R1 SD'),
                    field('Music folder on device', _musicDir, hint: 'Music'),
                    const SizedBox(height: 4),
                    Text('Output format', style: TextStyle(color: Theme.of(context).colorScheme.onSurface.withValues(alpha: .7))),
                    const SizedBox(height: 6),
                    DropdownButton<String>(
                      value: _format,
                      items: AppSettings.formats.map((f) => DropdownMenuItem(value: f, child: Text(f))).toList(),
                      onChanged: (v) => setState(() => _format = v ?? 'FLAC'),
                    ),
                    const SizedBox(height: 18),
                    const Divider(),
                    const SizedBox(height: 12),
                    Text('Tools', style: Theme.of(context).textTheme.titleMedium),
                    const SizedBox(height: 10),
                    field('yt-dlp executable', _ytdlp, hint: 'yt-dlp'),
                    field('ffmpeg executable (ffprobe is derived from it)', _ffmpeg, hint: 'ffmpeg'),
                    const SizedBox(height: 8),
                    FilledButton.icon(onPressed: save, icon: const Icon(Icons.save), label: const Text('Save settings')),
                    if (_saved.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 12),
                        child: Text(_saved, style: const TextStyle(color: Colors.greenAccent)),
                      ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
