// Thin wrapper around the /ws event bus, converting raw text frames to
// typed TcEvents.
import 'package:web_socket_channel/web_socket_channel.dart';

import 'events.dart';

class TcSocket {
  TcSocket({required String baseUrl})
      : _uri = Uri.parse('${baseUrl.replaceFirst('http', 'ws')}/ws');

  final Uri _uri;
  WebSocketChannel? _channel;

  // Connecting lazily keeps constructing an AppState free of network side
  // effects, which is what lets widget tests build one without hanging.
  WebSocketChannel get _connected => _channel ??= WebSocketChannel.connect(_uri);

  Stream<TcEvent> get events => _connected.stream
      .map((raw) => TcEvent.tryParse(raw as String))
      .where((e) => e != null)
      .cast<TcEvent>();

  void close() => _channel?.sink.close();
}
