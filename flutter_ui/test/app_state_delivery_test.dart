// A delivery_status event must flip the matching message's delivery_state in
// place so the per-row indicator updates live, and the message model must
// parse the field tolerantly (absent/null -> null).
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _channelHash = 'channel-delivery';
const _messageId = 'msg-1';

Map<String, Object?> _message({String? deliveryState}) => {
      'message_id': _messageId,
      'sender_hash': 'me',
      'sender_name': 'Me',
      'content': 'hello',
      'timestamp': 1000.0,
      'reply_to': null,
      'has_image': false,
      'reactions': <Object>[],
      'delivery_state': ?deliveryState,
    };

void main() {
  group('Message.fromJson delivery_state', () {
    test('parses a present value', () {
      final m = Message.fromJson(
          Map<String, dynamic>.from(_message(deliveryState: 'pending')));
      expect(m.deliveryState, 'pending');
    });

    test('is null when absent', () {
      final m = Message.fromJson(Map<String, dynamic>.from(_message()));
      expect(m.deliveryState, isNull);
    });
  });

  group('delivery_status event', () {
    late FakeBackend backend;
    late AppState state;

    setUp(() {
      backend = FakeBackend();
      state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
      state.selectedChannelHash = _channelHash;
      state.messagesByChannel[_channelHash] = [
        Message.fromJson(Map<String, dynamic>.from(_message(deliveryState: 'pending'))),
      ];
    });

    tearDown(() => state.dispose());

    test('updates the matching message in place', () {
      state.applyEvent(
          const DeliveryStatusEvent(_channelHash, _messageId, 'delivered'));

      expect(state.messagesByChannel[_channelHash]!.single.deliveryState, 'delivered');
    });

    test('notifies listeners on a change', () {
      var notified = 0;
      state.addListener(() => notified++);

      state.applyEvent(
          const DeliveryStatusEvent(_channelHash, _messageId, 'failed'));

      expect(notified, greaterThan(0));
      expect(state.messagesByChannel[_channelHash]!.single.deliveryState, 'failed');
    });

    test('an event for an unknown message is a no-op', () {
      state.applyEvent(
          const DeliveryStatusEvent(_channelHash, 'no-such-id', 'delivered'));

      // The known message keeps its state.
      expect(state.messagesByChannel[_channelHash]!.single.deliveryState, 'pending');
    });

    test('an event for a channel we never loaded is a no-op', () {
      state.applyEvent(
          const DeliveryStatusEvent('other-channel', _messageId, 'delivered'));

      expect(state.messagesByChannel.containsKey('other-channel'), isFalse);
    });
  });

  test('TcEvent.tryParse builds a DeliveryStatusEvent', () {
    final event = TcEvent.tryParse(
        '{"type":"delivery_status","channel_hash":"c","message_id":"m","delivery_state":"failed"}');
    expect(event, isA<DeliveryStatusEvent>());
    final d = event as DeliveryStatusEvent;
    expect(d.channelHash, 'c');
    expect(d.messageId, 'm');
    expect(d.deliveryState, 'failed');
  });
}
