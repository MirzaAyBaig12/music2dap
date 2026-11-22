class TrackInfo {
  TrackInfo({
    required this.id,
    this.title = '',
    this.artists = const [],
    this.album = '',
    this.albumArtist = '',
    this.date = '',
    this.coverUrl = '',
    this.trackNo = 0,
    this.trackTotal = 0,
    this.discNo = 0,
    this.discTotal = 0,
    this.durationSeconds = 0,
  });

  final String id;
  String title;
  List<String> artists;
  String album;
  String albumArtist;
  String date;
  String coverUrl;
  int trackNo;
  int trackTotal;
  int discNo;
  int discTotal;
  int durationSeconds;

  String get display => '${artists.join(', ')} — $title';

  String get searchQuery => '${artists.isEmpty ? '' : '${artists.first} '}$title audio';

  Map<String, String> get metadata => {
        'title': title,
        'artist': artists.join(', '),
        'albumartist': albumArtist.isEmpty
            ? (artists.isEmpty ? '' : artists.first)
            : albumArtist,
        'album': album,
        'date': date,
        'tracknumber': trackNo > 0 ? '$trackNo' : '',
        'tracktotal': trackTotal > 0 ? '$trackTotal' : '',
        'totaltracks': trackTotal > 0 ? '$trackTotal' : '',
        'discnumber': discNo > 0 ? '$discNo' : '',
        'disctotal': discTotal > 0 ? '$discTotal' : '',
        'SOURCE': 'https://open.spotify.com/track/$id',
        'comment': 'https://open.spotify.com/track/$id',
      };
}
