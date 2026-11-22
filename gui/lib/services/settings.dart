import 'dart:io';

import 'package:shared_preferences/shared_preferences.dart';

class AppSettings {
  AppSettings({
    this.mountRoot = '/run/media/\$USER',
    this.deviceMount = 'HiBy R1 SD',
    this.musicDir = 'Music',
    this.format = 'FLAC',
    this.ytdlp = 'yt-dlp',
    this.ffmpeg = 'ffmpeg',
  });

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

  String get libraryPath => '$resolvedMountRoot/$deviceMount/$musicDir';

  static const formats = ['FLAC', 'MP3', 'OPUS'];

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('mountRoot', mountRoot);
    await p.setString('deviceMount', deviceMount);
    await p.setString('musicDir', musicDir);
    await p.setString('format', format);
    await p.setString('ytdlp', ytdlp);
    await p.setString('ffmpeg', ffmpeg);
  }

  static Future<AppSettings> load() async {
    final p = await SharedPreferences.getInstance();
    return AppSettings(
      mountRoot: p.getString('mountRoot') ?? '/run/media/\$USER',
      deviceMount: p.getString('deviceMount') ?? 'HiBy R1 SD',
      musicDir: p.getString('musicDir') ?? 'Music',
      format: p.getString('format') ?? 'FLAC',
      ytdlp: p.getString('ytdlp') ?? 'yt-dlp',
      ffmpeg: p.getString('ffmpeg') ?? 'ffmpeg',
    );
  }
}
