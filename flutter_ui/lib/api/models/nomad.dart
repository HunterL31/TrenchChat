// Nomad Network browsing models, mirroring the /nomad/* endpoints in
// devtools/testenv/api.py.
import 'dart:convert';

class NomadNode {
  const NomadNode({
    required this.nodeHash,
    required this.displayName,
    required this.firstSeen,
    required this.lastSeen,
  });

  final String nodeHash;

  /// From unsigned announce app_data: presentation only. The UI always shows
  /// the hash beside it, since any node can claim any name.
  final String displayName;
  final double firstSeen;
  final double lastSeen;

  factory NomadNode.fromJson(Map<String, dynamic> json) => NomadNode(
        nodeHash: json['node_hash'] as String,
        displayName: json['display_name'] as String? ?? '',
        firstSeen: (json['first_seen'] as num).toDouble(),
        lastSeen: (json['last_seen'] as num).toDouble(),
      );
}

class NomadBookmark {
  const NomadBookmark({
    required this.nodeHash,
    required this.path,
    required this.label,
    required this.addedAt,
  });

  final String nodeHash;
  final String path;
  final String label;
  final double addedAt;

  factory NomadBookmark.fromJson(Map<String, dynamic> json) => NomadBookmark(
        nodeHash: json['node_hash'] as String,
        path: json['path'] as String,
        label: json['label'] as String? ?? '',
        addedAt: (json['added_at'] as num).toDouble(),
      );
}

class NomadPage {
  const NomadPage({required this.contentB64, required this.fetchedAt});

  final String contentB64;
  final double fetchedAt;

  /// The micron source. Tolerates malformed UTF-8 and bad base64 -- a page a
  /// node served is displayed best-effort, never thrown on.
  String get source {
    try {
      return utf8.decode(base64Decode(contentB64), allowMalformed: true);
    } catch (_) {
      return '';
    }
  }

  factory NomadPage.fromJson(Map<String, dynamic> json) => NomadPage(
        contentB64: json['content_b64'] as String? ?? '',
        fetchedAt: (json['fetched_at'] as num?)?.toDouble() ?? 0,
      );
}

class NomadHostedEntry {
  const NomadHostedEntry({required this.path, required this.size});

  final String path;
  final int size;

  factory NomadHostedEntry.fromJson(Map<String, dynamic> json) =>
      NomadHostedEntry(
        path: json['path'] as String,
        size: (json['size'] as num?)?.toInt() ?? 0,
      );
}

class NomadHosting {
  const NomadHosting({
    required this.enabled,
    required this.nodeName,
    required this.nodeHash,
    required this.pagesDir,
    required this.pages,
    required this.files,
  });

  final bool enabled;
  final String nodeName;

  /// The destination hash our own pages are served under -- what to browse
  /// to read them back.
  final String nodeHash;
  final String pagesDir;
  final List<NomadHostedEntry> pages;
  final List<NomadHostedEntry> files;

  factory NomadHosting.fromJson(Map<String, dynamic> json) => NomadHosting(
        enabled: json['enabled'] as bool? ?? false,
        nodeName: json['node_name'] as String? ?? '',
        nodeHash: json['node_hash'] as String? ?? '',
        pagesDir: json['pages_dir'] as String? ?? '',
        pages: [
          for (final entry in (json['pages'] as List<dynamic>? ?? []))
            NomadHostedEntry.fromJson(entry as Map<String, dynamic>)
        ],
        files: [
          for (final entry in (json['files'] as List<dynamic>? ?? []))
            NomadHostedEntry.fromJson(entry as Map<String, dynamic>)
        ],
      );
}

/// Live state of one fetch, updated from nomad_fetch WS events.
class NomadFetchStatus {
  const NomadFetchStatus({
    required this.nodeHash,
    required this.path,
    required this.status,
    required this.progress,
    this.reason,
  });

  final String nodeHash;
  final String path;

  /// 'queued' | 'fetching' | 'done' | 'failed'
  final String status;
  final double progress;
  final String? reason;

  bool get isTerminal => status == 'done' || status == 'failed';
}
