// Watching one share: the socket that carries its updates and the frame
// buffer the stage paints.
//
// Opening the socket is what subscribes this node to the share, closing it is
// what unsubscribes. Each binary message is one update; the client decodes
// its images, paints, and only then answers with a text frame, which is the
// credit for the next. Nothing arrives that this client has not made room
// for, so a slow machine gets fewer, larger updates rather than a queue.
import 'dart:async';
import 'dart:convert';
import 'dart:ui' as ui;

import 'package:flutter/foundation.dart';
import 'package:stream_channel/stream_channel.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'screen_wire.dart';

/// The latest decoded image per tile, or a full frame that supersedes them.
///
/// Bounded by construction: one image per grid slot plus one full frame,
/// however long a watch runs. Painters read it; [ScreenWatch] writes it.
class ScreenFrameBuffer extends ChangeNotifier {
  int width = 0;
  int height = 0;
  int tileShift = 7;
  int seq = 0;
  int updates = 0;
  int cursorX = -1;
  int cursorY = -1;
  ui.Image? full;
  final Map<int, ui.Image> tiles = {};

  bool get isEmpty => full == null && tiles.isEmpty;
  int get tileEdge => 1 << tileShift;
  int get cols => (width + tileEdge - 1) ~/ tileEdge;

  /// Applies one decoded update: a full frame replaces everything, a tile
  /// replaces its slot.
  void apply(ScreenUpdate update, List<ui.Image> images) {
    if (update.width != width || update.height != height ||
        update.tileShift != tileShift) {
      clear();
      width = update.width;
      height = update.height;
      tileShift = update.tileShift;
    }
    seq = update.seq;
    updates += 1;
    cursorX = update.cursorX;
    cursorY = update.cursorY;
    if (update.isFull) {
      full?.dispose();
      full = images.first;
      for (final tile in tiles.values) {
        tile.dispose();
      }
      tiles.clear();
    } else {
      for (var i = 0; i < update.entries.length; i++) {
        final entry = update.entries[i];
        final slot = entry.ty * cols + entry.tx;
        tiles[slot]?.dispose();
        tiles[slot] = images[i];
      }
    }
    notifyListeners();
  }

  void clear() {
    full?.dispose();
    full = null;
    for (final tile in tiles.values) {
      tile.dispose();
    }
    tiles.clear();
    seq = 0;
    cursorX = -1;
    cursorY = -1;
    notifyListeners();
  }

  @override
  void dispose() {
    clear();
    super.dispose();
  }
}

/// Decodes one update's images, each bounded to the size its slot declares,
/// so a JPEG cannot cost more than the box it is drawn into.
Future<List<ui.Image>> decodeScreenUpdate(ScreenUpdate update) async {
  final images = <ui.Image>[];
  for (final entry in update.entries) {
    final rect = update.isFull
        ? (left: 0, top: 0, right: update.width, bottom: update.height)
        : update.tileRect(entry.tx, entry.ty);
    final codec = await ui.instantiateImageCodec(
      entry.bytes,
      targetWidth: rect.right - rect.left,
      targetHeight: rect.bottom - rect.top,
    );
    try {
      final frame = await codec.getNextFrame();
      images.add(frame.image);
    } finally {
      codec.dispose();
    }
  }
  return images;
}

/// Why a watch ended, as the socket reported it.
typedef ScreenWatchEnded = void Function(String reason);

/// Builds the watch for one peer; AppState takes one so tests can inject a
/// watch over a fake channel.
typedef ScreenWatchFactory = ScreenWatch Function(String peer);

/// One open watch: the socket, the decode, the credit.
class ScreenWatch {
  ScreenWatch({
    required String baseUrl,
    required this.peer,
    String token = '',
    StreamChannel<dynamic> Function(Uri)? connect,
  })  : _uri = Uri.parse('${baseUrl.replaceFirst('http', 'ws')}/screen/watch/$peer'
            '${token.isEmpty ? '' : '?token=${Uri.encodeQueryComponent(token)}'}'),
        _connect = connect ?? WebSocketChannel.connect;

  final String peer;
  final Uri _uri;
  final StreamChannel<dynamic> Function(Uri) _connect;
  final ScreenFrameBuffer buffer = ScreenFrameBuffer();

  StreamChannel<dynamic>? _channel;
  StreamSubscription? _sub;
  bool _closed = false;
  String endedReason = '';

  /// Called once when the backend says the share is over or was refused.
  ScreenWatchEnded? onEnded;

  void start() {
    if (_channel != null || _closed) return;
    _channel = _connect(_uri);
    _sub = _channel!.stream.listen(
      (message) => unawaited(_onMessage(message)),
      onDone: () => _end('closed'),
      onError: (_) => _end('closed'),
    );
  }

  Future<void> _onMessage(dynamic message) async {
    if (_closed) return;
    if (message is String) {
      Map<String, dynamic> body;
      try {
        body = jsonDecode(message) as Map<String, dynamic>;
      } catch (_) {
        return;
      }
      _end(body['ended'] as String? ?? 'stopped');
      return;
    }
    final bytes = message is Uint8List
        ? message
        : Uint8List.fromList(message as List<int>);
    try {
      final update = parseScreenUpdate(bytes);
      final images = await decodeScreenUpdate(update);
      if (_closed) {
        for (final image in images) {
          image.dispose();
        }
        return;
      }
      buffer.apply(update, images);
    } catch (e) {
      debugPrint('screen watch: dropped an update: $e');
    }
    // The credit for the next update, sent after the paint has what it needs.
    _channel?.sink.add('r');
  }

  void _end(String reason) {
    if (_closed) return;
    _closed = true;
    endedReason = reason;
    _sub?.cancel();
    _channel?.sink.close();
    _channel = null;
    onEnded?.call(reason);
  }

  void close() {
    _end('closed');
    buffer.dispose();
  }
}
