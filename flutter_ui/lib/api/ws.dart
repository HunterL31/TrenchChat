// Thin wrapper around the /ws event bus, converting raw text frames to
// typed TcEvents.
import 'package:web_socket_channel/web_socket_channel.dart';

import 'events.dart';

class TcSocket {
  TcSocket({required String baseUrl})
      : _channel = WebSocketChannel.connect(
          Uri.parse('${baseUrl.replaceFirst('http', 'ws')}/ws'),
        );

  final WebSocketChannel _channel;

  Stream<TcEvent> get events => _channel.stream
      .map((raw) => TcEvent.tryParse(raw as String))
      .where((e) => e != null)
      .cast<TcEvent>();

  void close() => _channel.sink.close();
}
