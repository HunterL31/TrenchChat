// The file card in a message row: one state at a time, nothing fetched
// without the reader asking, and a refused manifest that says so rather than
// leaving the message looking like it never had one.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/screens/main_window/message_list.dart';

final _fileHash = 'ab' * 32;

Message _msg({
  FileAttachment? file,
  bool fileStripped = false,
}) =>
    Message(
      messageId: 'm1',
      senderHash: 'peer',
      senderName: 'peer',
      content: 'here it is',
      timestamp: 1700000000.0,
      replyTo: null,
      hasImage: false,
      reactions: const [],
      file: file,
      fileStripped: fileStripped,
    );

FileAttachment _file({
  String state = fileStateAvailable,
  double progress = 0.0,
  String? reason,
}) =>
    FileAttachment(
      name: 'notes.bin',
      size: 150000,
      hash: _fileHash,
      state: state,
      progress: progress,
      reason: reason,
    );

Widget _harness(
  Message message, {
  void Function(String messageId, String fileHash)? onFetchFile,
  void Function(String fileHash, String fileName)? onSaveFile,
}) =>
    MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 400,
          child: MessageList(
            messages: [message],
            meHashHex: 'me',
            displayNameFor: (hash, fallback) => fallback,
            onFetchFile: onFetchFile,
            onSaveFile: onSaveFile,
          ),
        ),
      ),
    );

void main() {
  testWidgets('a manifest nobody asked for offers a download', (tester) async {
    await tester.pumpWidget(_harness(_msg(file: _file()), onFetchFile: (_, _) {}));

    expect(find.text('notes.bin'), findsOneWidget);
    expect(find.text('146.5 KB'), findsOneWidget);
    expect(find.text('DOWNLOAD'), findsOneWidget);
    expect(find.byType(LinearProgressIndicator), findsNothing);
  });

  testWidgets('Download asks for that message\'s file', (tester) async {
    final asked = <({String messageId, String fileHash})>[];
    await tester.pumpWidget(_harness(
      _msg(file: _file()),
      onFetchFile: (messageId, fileHash) =>
          asked.add((messageId: messageId, fileHash: fileHash)),
    ));

    await tester.tap(find.text('DOWNLOAD'));
    await tester.pumpAndSettle();

    expect(asked, hasLength(1));
    expect(asked.single.messageId, 'm1');
    expect(asked.single.fileHash, _fileHash);
  });

  testWidgets('a download in flight shows a bar and its percentage',
      (tester) async {
    await tester.pumpWidget(_harness(
        _msg(file: _file(state: fileStateFetching, progress: 0.42))));

    expect(find.text('42%'), findsOneWidget);
    expect(find.byType(LinearProgressIndicator), findsOneWidget);
    expect(find.text('DOWNLOAD'), findsNothing);

    final bar =
        tester.widget<LinearProgressIndicator>(find.byType(LinearProgressIndicator));
    expect(bar.value, 0.42);
  });

  testWidgets('a queued download reads as in flight too', (tester) async {
    await tester.pumpWidget(_harness(_msg(file: _file(state: fileStateQueued))));

    expect(find.text('0%'), findsOneWidget);
    expect(find.byType(LinearProgressIndicator), findsOneWidget);
  });

  testWidgets('a held file offers a save, and Save calls through',
      (tester) async {
    final saved = <({String hash, String name})>[];
    await tester.pumpWidget(_harness(
      _msg(file: _file(state: fileStateDone, progress: 1.0)),
      onSaveFile: (hash, name) => saved.add((hash: hash, name: name)),
    ));

    expect(find.text('SAVE'), findsOneWidget);
    await tester.tap(find.text('SAVE'));
    await tester.pumpAndSettle();

    expect(saved.single.hash, _fileHash);
    expect(saved.single.name, 'notes.bin');
  });

  testWidgets('nobody holding it yet is stated plainly, not as an error',
      (tester) async {
    await tester
        .pumpWidget(_harness(_msg(file: _file(state: fileStateUnavailable))));

    expect(find.text('Waiting for a member who has this file'), findsOneWidget);
    expect(find.text('DOWNLOAD'), findsNothing);
    expect(find.text('RETRY'), findsNothing);
  });

  testWidgets('a failed download names the reason and offers a retry',
      (tester) async {
    final asked = <String>[];
    await tester.pumpWidget(_harness(
      _msg(file: _file(state: fileStateFailed, reason: 'corrupt')),
      onFetchFile: (messageId, _) => asked.add(messageId),
    ));

    expect(find.textContaining('Download failed'), findsOneWidget);
    expect(find.textContaining('did not verify'), findsOneWidget);

    await tester.tap(find.text('RETRY'));
    await tester.pumpAndSettle();

    expect(asked, ['m1']);
  });

  testWidgets('a refused manifest says so instead of showing a card',
      (tester) async {
    await tester.pumpWidget(_harness(_msg(fileStripped: true)));

    expect(find.text('Attachment refused'), findsOneWidget);
    expect(find.text('DOWNLOAD'), findsNothing);
  });

  testWidgets('a message with no file renders no card at all', (tester) async {
    await tester.pumpWidget(_harness(_msg()));

    expect(find.text('notes.bin'), findsNothing);
    expect(find.text('DOWNLOAD'), findsNothing);
    expect(find.text('Attachment refused'), findsNothing);
  });

  testWidgets('with no callbacks wired the buttons are inert', (tester) async {
    await tester.pumpWidget(_harness(_msg(file: _file())));

    await tester.tap(find.text('DOWNLOAD'));
    await tester.pumpAndSettle();
    // Nothing to assert but the absence of a crash: a null callback must not
    // be called, and the card must still render.
    expect(find.text('DOWNLOAD'), findsOneWidget);
  });
}
