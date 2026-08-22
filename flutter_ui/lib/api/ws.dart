// Thin wrapper around the /ws event bus, converting raw text frames to
// typed TcEvents. Reconnects itself with exponential backoff when the
// connection drops -- without this, one WS hiccup silently freezes every
// live update until the page is reloaded.
import 'dart:async';

import 'package:web_socket_channel/web_socket_channel.dart';

import 'events.dart';

const Duration _reconnectBaseDelay = Duration(seconds: 1);
const Duration _reconnectMaxDelay = Duration(seconds: 30);

/// The backend event socket's state, distinct from the mesh link quality:
/// this says whether live updates are flowing, not how the radio is doing.
enum TcConnState { connected, reconnecting, disconnected }

class TcSocket {
  /// The token goes in the query string because a browser cannot set headers
  /// on a WebSocket handshake, and this socket carries every inbound message.
  TcSocket({required String baseUrl, String token = ''})
      : _uri = Uri.parse('${baseUrl.replaceFirst('http', 'ws')}/ws'
            '${token.isEmpty ? '' : '?token=${Uri.encodeQueryComponent(token)}'}');

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

  /// Fired whenever the backend-socket connection state changes. The state
  /// layer surfaces it so the UI can show a "reconnecting…/offline" hint,
  /// separate from the mesh link pill.
  void Function(TcConnState)? onConnStateChanged;

  TcConnState _connState = TcConnState.disconnected;
  TcConnState get connState => _connState;

  void _setConnState(TcConnState state) {
    if (_connState == state) return;
    _connState = state;
    onConnStateChanged?.call(state);
  }

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
      _setConnState(TcConnState.reconnecting);
      _scheduleReconnect();
    }

    final channel = WebSocketChannel.connect(_uri);
    _channel = channel;
    channel.ready.then((_) {
      if (_closed) return;
      _attempt = 0;
      _setConnState(TcConnState.connected);
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
    _setConnState(TcConnState.disconnected);
    _reconnectTimer?.cancel();
    _channel?.sink.close();
    _events.close();
  }
}
