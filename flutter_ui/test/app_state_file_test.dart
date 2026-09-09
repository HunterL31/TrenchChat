// The file half of a message, end to end through AppState: the model parses
// the manifest api.py sends, a file_fetch event moves the card without a
// refetch, sharing carries the bytes, saving hands them to the injected
// saver, and every refusal reason reads as something a person can act on.
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/api/models/permissions.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _channelHash = 'channel-files';
const _messageId = 'msg-file-1';
final _fileHash = 'ab' * 32;
final _otherHash = 'cd' * 32;

Map<String, Object?> _message({Map<String, Object?>? file, bool stripped = false}) => {
      'message_id': _messageId,
      'sender_hash': 'peer',
      'sender_name': 'Peer',
      'content': 'here it is',
      'timestamp': 1000.0,
      'reply_to': null,
      'has_image': false,
      'file': file,
      'file_stripped': stripped,
      'reactions': <Object>[],
    };

Map<String, Object?> _file({
  String state = fileStateAvailable,
  double progress = 0.0,
  String? reason,
}) =>
    {
      'name': 'notes.bin',
      'size': 150000,
      'hash': _fileHash,
      'state': state,
      'progress': progress,
      'reason': reason,
    };

void main() {
  group('Message.fromJson file', () {
    test('parses the manifest and its download state', () {
      final m = Message.fromJson(Map<String, dynamic>.from(
          _message(file: _file(state: fileStateFetching, progress: 0.25))));

      expect(m.file, isNotNull);
      expect(m.file!.name, 'notes.bin');
      expect(m.file!.size, 150000);
      expect(m.file!.hash, _fileHash);
      expect(m.file!.state, fileStateFetching);
      expect(m.file!.progress, 0.25);
      expect(m.file!.reason, isNull);
      expect(m.fileStripped, isFalse);
    });

    test('a null file is no file, and a stripped manifest says so', () {
      final m = Message.fromJson(Map<String, dynamic>.from(_message(stripped: true)));

      expect(m.file, isNull);
      expect(m.fileStripped, isTrue);
    });

    test('a failed download keeps its reason', () {
      final m = Message.fromJson(Map<String, dynamic>.from(
          _message(file: _file(state: fileStateFailed, reason: 'refused'))));

      expect(m.file!.state, fileStateFailed);
      expect(m.file!.reason, 'refused');
    });
  });

  group('file_fetch', () {
    test('parses into a FileFetchEvent', () {
      final event = TcEvent.tryParse(jsonEncode({
        'type': 'file_fetch',
        'file_hash': _fileHash,
        'message_ids': [_messageId],
        'channels': [_channelHash],
        'state': fileStateFetching,
        'progress': 0.5,
        'reason': null,
      }));

      expect(
        event,
        isA<FileFetchEvent>()
            .having((e) => e.fileHash, 'fileHash', _fileHash)
            .having((e) => e.messageIds, 'messageIds', [_messageId])
            .having((e) => e.channels, 'channels', [_channelHash])
            .having((e) => e.state, 'state', fileStateFetching)
            .having((e) => e.progress, 'progress', 0.5),
      );
    });
  });

  group('AppState', () {
    late FakeBackend backend;
    late AppState state;
    final saved = <({String name, Uint8List bytes, String mimeType})>[];

    setUp(() {
      saved.clear();
      backend = FakeBackend();
      state = AppState(
        baseUrl: backend.baseUrl,
        httpClient: backend.client(),
        saveFileBytes: (name, bytes, mimeType) async =>
            saved.add((name: name, bytes: bytes, mimeType: mimeType)),
      );
      state.selectedChannelHash = _channelHash;
      state.messagesByChannel[_channelHash] = [
        Message.fromJson(Map<String, dynamic>.from(_message(file: _file()))),
      ];
    });

    tearDown(() => state.dispose());

    FileAttachment fileOnScreen() => state.messagesByChannel[_channelHash]!.single.file!;

    test('a file_fetch event moves the card in place', () {
      state.applyEvent(FileFetchEvent(
          _fileHash, const [_messageId], const [_channelHash], fileStateFetching, 0.4, null));

      expect(fileOnScreen().state, fileStateFetching);
      expect(fileOnScreen().progress, 0.4);
      // The manifest itself is untouched: the event carries no name or size.
      expect(fileOnScreen().name, 'notes.bin');
      expect(backend.requests, isEmpty);
    });

    test('an event for another file leaves this card alone', () {
      state.applyEvent(FileFetchEvent(
          _otherHash, const [], const [_channelHash], fileStateDone, 1.0, null));

      expect(fileOnScreen().state, fileStateAvailable);
    });

    test('progress never goes backwards', () {
      state.applyEvent(FileFetchEvent(
          _fileHash, const [_messageId], const [], fileStateFetching, 0.6, null));
      state.applyEvent(FileFetchEvent(
          _fileHash, const [_messageId], const [], fileStateFetching, 0.2, null));

      expect(fileOnScreen().progress, 0.6);
    });

    test('fetchFile starts the download and applies the snapshot it answers with',
        () async {
      backend.routes['POST /channels/$_channelHash/files/$_fileHash/fetch'] = {
        'ok': true,
        'file_hash': _fileHash,
        'state': fileStateQueued,
        'progress': 0.0,
        'reason': null,
        'message_ids': [_messageId],
        'channels': [_channelHash],
      };

      expect(await state.fetchFile(_channelHash, _fileHash, _messageId), isTrue);

      final sent = backend.requests.single;
      expect(sent.method, 'POST');
      expect(jsonDecode(sent.body)['message_id'], _messageId);
      expect(fileOnScreen().state, fileStateQueued);
    });

    test('a file the backend will not answer for reports it', () async {
      backend.routes['POST /channels/$_channelHash/files/$_fileHash/fetch'] =
          const FakeError(404, {'ok': false, 'error': 'no such file'});

      expect(await state.fetchFile(_channelHash, _fileHash, _messageId), isFalse);
      expect(state.takeActionError(), contains('no longer available'));
    });

    test('shareFile sends the name and the bytes as base64', () async {
      backend.routes['POST /channels/$_channelHash/messages'] = {'ok': true};
      backend.routes['GET /channels/$_channelHash/messages'] = <Object>[];

      expect(
        await state.shareFile('notes.bin', Uint8List.fromList([1, 2, 3]), content: 'take it'),
        isTrue,
      );

      final sent = backend.requests
          .lastWhere((r) => r.method == 'POST' && r.path.endsWith('/messages'));
      final body = jsonDecode(sent.body) as Map<String, dynamic>;
      expect(body['file_name'], 'notes.bin');
      expect(body['file_data_b64'], base64Encode([1, 2, 3]));
      expect(body['content'], 'take it');
      expect(body.containsKey('image_data_b64'), isFalse);
    });

    test('a refused share reports the reason in words', () async {
      backend.routes['POST /channels/$_channelHash/messages'] = {
        'ok': false,
        'reason': 'no_share_permission',
      };

      expect(await state.shareFile('notes.bin', Uint8List.fromList([1])), isFalse);
      expect(state.takeActionError(),
          'You do not have permission to share files in this channel.');
    });

    test('a 400 refusal is an answer, not an exception', () async {
      backend.routes['POST /channels/$_channelHash/messages'] =
          const FakeError(400, {'ok': false, 'reason': 'file_too_large', 'error': 'too big'});

      expect(await state.shareFile('notes.bin', Uint8List.fromList([1])), isFalse);
      expect(state.takeActionError(), contains('over the size'));
    });

    test('saveFile hands the bytes, the name and a guessed type to the saver',
        () async {
      backend.routes['GET /channels/$_channelHash/files/$_fileHash'] = <String, Object>{};

      expect(await state.saveFile(_channelHash, _fileHash, 'notes.json'), isTrue);

      expect(saved, hasLength(1));
      expect(saved.single.name, 'notes.json');
      expect(saved.single.mimeType, 'application/json');
      expect(utf8.decode(saved.single.bytes), '{}');
    });

    test('saving a file this node does not hold saves nothing', () async {
      backend.routes['GET /channels/$_channelHash/files/$_fileHash'] =
          const FakeError(404, {'error': 'no such file'});

      expect(await state.saveFile(_channelHash, _fileHash, 'notes.bin'), isFalse);
      expect(saved, isEmpty);
      expect(state.takeActionError(), contains('not here yet'));
    });

    test('the share ceiling comes from the backend, with a default until it does',
        () async {
      expect(state.maxFileBytes, 5 * 1024 * 1024);
      backend.routes['GET /files/usage'] = {
        'usage': {'own': 0, 'received': 0, 'partial': 0},
        'limits': {'own': 1, 'received': 2, 'partial': 3},
        'max_file_bytes': 1024,
      };

      await state.loadFileUsage();

      expect(state.maxFileBytes, 1024);
    });

    test('a backend without the usage endpoint keeps the client ceiling', () async {
      await state.loadFileUsage();

      expect(state.maxFileBytes, 5 * 1024 * 1024);
    });

    test('canShareFiles reads the channel permission and fails closed', () {
      expect(state.canShareFiles, isFalse);

      state.permissionsByChannel[_channelHash] = const ChannelPermissions(
        invite: false,
        kick: false,
        manageRoles: false,
        manageChannel: false,
        sendMessage: true,
        shareFiles: true,
        voiceChat: false,
      );

      expect(state.canShareFiles, isTrue);
    });
  });

  group('refusal reasons', () {
    test('every backend reason reads as something a person can act on', () {
      expect(sendRefusalMessage('no_share_permission'),
          'You do not have permission to share files in this channel.');
      expect(sendRefusalMessage('storage'), 'Not enough file storage on this node.');
      expect(sendRefusalMessage('open_join_channel'),
          'Files are shared in invite-only channels only.');
      expect(sendRefusalMessage('file_and_image'),
          'A message carries an image or a file, not both.');
      expect(sendRefusalMessage('empty_file'), 'That file is empty.');
      expect(sendRefusalMessage('bad_manifest'), 'That file could not be shared.');
      expect(sendRefusalMessage('no_channel'), 'That channel is not known here.');
      expect(sendRefusalMessage('incomplete_file'), 'That file could not be read.');
      expect(sendRefusalMessage('bad_file_base64'), 'That file could not be read.');
      expect(sendRefusalMessage('no_file_in_dm'),
          'Files are not shared in direct messages.');
      expect(sendRefusalMessage('no_send_permission'),
          "You don't have permission to send in this channel.");
      expect(sendRefusalMessage('no_recipients'), contains('no known subscribers'));
    });

    test('an unknown reason says only that it did not go', () {
      expect(sendRefusalMessage('something_new'), 'Message was not sent.');
      expect(sendRefusalMessage(null), 'Message was not sent.');
    });
  });
}
