import 'dart:io';

import 'package:shared_preferences/shared_preferences.dart';

String _joinPath(String a, String b) {
  if (a.isEmpty) return b;
  if (b.isEmpty) return a;
  if (a.endsWith('/') || a.endsWith('\\')) return '$a$b';
  return '$a${Platform.pathSeparator}$b';
}

String _exeDir() {
  try {
    final p = Platform.resolvedExecutable;
    final i = p.lastIndexOf(RegExp(r'[\\/]'));
    return i >= 0 ? p.substring(0, i) : '.';
  } catch (_) {
    return '.';
  }
}

String _findBundled(List<String> names) {
  final dir = _exeDir();
  final tools = _joinPath(dir, 'tools');
  for (final name in names) {
    final inTools = _joinPath(tools, name);
    if (File(inTools).existsSync()) return inTools;
    final besideExe = _joinPath(dir, name);
    if (File(besideExe).existsSync()) return besideExe;
  }
  return '';
}

/// Tools shipped next to the app by the Windows installer.
class BundledTools {
  static String get ytdlp =>
      _findBundled(Platform.isWindows ? ['yt-dlp.exe'] : ['yt-dlp']);
  static String get ffmpeg =>
      _findBundled(Platform.isWindows ? ['ffmpeg.exe'] : ['ffmpeg']);
}

class ToolCheck {
  const ToolCheck(this.name, this.command, {this.ok = false, this.detail = ''});

  final String name;
  final String command;
  final bool ok;
  final String detail;

  static Future<ToolCheck> run(
    String name,
    String command,
    List<String> args,
  ) async {
    try {
      final r = await Process.run(
        command,
        args,
      ).timeout(const Duration(seconds: 20));
      final out = '$r.stdout\n$r.stderr'.trim();
      final first = out
          .split('\n')
          .map((s) => s.trim())
          .where((s) => s.isNotEmpty)
          .toList();
      return ToolCheck(
        name,
        command,
        ok: r.exitCode == 0,
        detail: first.isEmpty ? 'exit ${r.exitCode}' : first.first,
      );
    } catch (e) {
      return ToolCheck(name, command, detail: '$e');
    }
  }
}

class AppSettings {
  AppSettings({
    String? mountRoot,
    String? deviceMount,
    this.musicDir = 'Music',
    this.format = 'FLAC',
    String? ytdlp,
    String? ffmpeg,
    this.playerPath = '',
  }) : mountRoot =
           mountRoot ?? (Platform.isWindows ? r'D:\' : r'/run/media/$USER'),
       deviceMount = deviceMount ?? (Platform.isWindows ? '' : 'HiBy R1 SD'),
       ytdlp =
           ytdlp ??
           (BundledTools.ytdlp.isEmpty ? 'yt-dlp' : BundledTools.ytdlp),
       ffmpeg =
           ffmpeg ??
           (BundledTools.ffmpeg.isEmpty ? 'ffmpeg' : BundledTools.ffmpeg);

  /// Full path to the DAP music folder. When set, this overrides the
  /// mount root / device / music folder fields below.
  String playerPath;
  String mountRoot;
  String deviceMount;
  String musicDir;
  String format;
  String ytdlp;
  String ffmpeg;

  String get resolvedMountRoot => mountRoot.replaceAll(
    '\$USER',
    Platform.environment['USER'] ?? Platform.environment['USERNAME'] ?? '',
  );

  String get libraryPath {
    if (playerPath.trim().isNotEmpty) return playerPath.trim();
    return _joinPath(
      _joinPath(resolvedMountRoot, deviceMount),
      musicDir.trim(),
    );
  }

  static const formats = ['FLAC', 'MP3', 'OPUS'];

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('playerPath', playerPath);
    await p.setString('mountRoot', mountRoot);
    await p.setString('deviceMount', deviceMount);
    await p.setString('musicDir', musicDir);
    await p.setString('format', format);
    await p.setString('ytdlp', ytdlp);
    await p.setString('ffmpeg', ffmpeg);
  }

  static String _tool(String? saved, String fallback, String legacy) {
    if (saved == null || saved.trim().isEmpty || saved == legacy) {
      return fallback.isEmpty ? legacy : fallback;
    }
    return saved;
  }

  static Future<AppSettings> load() async {
    final p = await SharedPreferences.getInstance();
    final bundledYt = BundledTools.ytdlp;
    final bundledFf = BundledTools.ffmpeg;
    return AppSettings(
      playerPath: p.getString('playerPath') ?? '',
      mountRoot: p.getString('mountRoot'),
      deviceMount: p.getString('deviceMount'),
      musicDir: p.getString('musicDir') ?? 'Music',
      format: p.getString('format') ?? 'FLAC',
      ytdlp: _tool(p.getString('ytdlp'), bundledYt, 'yt-dlp'),
      ffmpeg: _tool(p.getString('ffmpeg'), bundledFf, 'ffmpeg'),
    );
  }
}
