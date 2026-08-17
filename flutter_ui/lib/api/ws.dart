// Thin wrapper around the /ws event bus, converting raw text frames to
// typed TcEvents. Reconnects itself with exponential backoff when the
// connection drops -- without this, one WS hiccup silently freezes every
// live update until the page is reloaded.
import 'dart:async';

import 'package:web_socket_channel/web_socket_channel.dart';

import 'events.dart';

const Duration _reconnectBaseDelay = Duration(seconds: 1);
const Duration _reconnectMaxDelay = Duration(seconds: 30);

class TcSocket {
  TcSocket({required String baseUrl})
      : _uri = Uri.parse('${baseUrl.replaceFirst('http', 'ws')}/ws');

  final Uri _uri;
  final StreamController<TcEvent> _events = StreamController.broadcast();

  WebSocketChannel? _channel;
  Timer? _reconnectTimer;
  bool _started = false;
  bool _closed = false;
  bool _everConnected = false;
  int _attempt = 0;

  /// Called after a *re*connect succeeds (not the first connect). The state
  /// layer uses it to re-fetch anything whose events were missed while down.
  void Function()? onReconnected;

  // Connecting lazily keeps constructing an AppState free of network side
  // effects, which is what lets widget tests build one without hanging.
  Stream<TcEvent> get events {
    if (!_started) {
      _started = true;
      _connect();
    }
    return _events.stream;
  }

  void _connect() {
    if (_closed) return;
    var lost = false;
    void onConnectionLost() {
      if (lost || _closed) return;
      lost = true;
      _scheduleReconnect();
    }

    final channel = WebSocketChannel.connect(_uri);
    _channel = channel;
    channel.ready.then((_) {
      if (_closed) return;
      _attempt = 0;
      final isReconnect = _everConnected;
      _everConnected = true;
      if (isReconnect) onReconnected?.call();
    }).catchError((_) => onConnectionLost());

    channel.stream.listen(
      (raw) {
        final event = TcEvent.tryParse(raw as String);
        if (event != null) _events.add(event);
      },
      onError: (_) => onConnectionLost(),
      onDone: onConnectionLost,
      cancelOnError: true,
    );
  }

  void _scheduleReconnect() {
    if (_closed) return;
    final delay = _reconnectBaseDelay * (1 << _attempt.clamp(0, 5));
    _attempt += 1;
    _reconnectTimer = Timer(
      delay > _reconnectMaxDelay ? _reconnectMaxDelay : delay,
      _connect,
    );
  }

  void close() {
    _closed = true;
    _reconnectTimer?.cancel();
    _channel?.sink.close();
    _events.close();
  }
}
