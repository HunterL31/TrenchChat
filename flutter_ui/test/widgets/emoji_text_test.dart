import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/emoji.dart';
import 'package:flutter_ui/widgets/emoji_text.dart';

const _hash =
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

CustomEmoji _emoji(String name) => CustomEmoji(
      emojiHash: _hash,
      name: name,
      imageBytes: Uint8List.fromList([1, 2, 3]),
    );

const _style = TextStyle(fontSize: 14);

void main() {
  test('a hash token resolves to an inline image span', () {
    final spans = emojiSpans('hi :salute@$_hash: there', {_hash: _emoji('salute')}, _style);
    expect(spans, hasLength(3));
    expect((spans[0] as TextSpan).text, 'hi ');
    expect(spans[1], isA<WidgetSpan>());
    expect((spans[2] as TextSpan).text, ' there');
  });

  test('a legacy name token resolves by exact name', () {
    final spans = emojiSpans(':salute:', {_hash: _emoji('salute')}, _style);
    expect(spans, hasLength(1));
    expect(spans.single, isA<WidgetSpan>());
  });

  test('unknown tokens stay literal text', () {
    final spans = emojiSpans('a :nope: b', {_hash: _emoji('salute')}, _style);
    expect(spans, hasLength(1));
    expect((spans.single as TextSpan).text, 'a :nope: b');
  });

  test('an empty library passes content through untouched', () {
    final spans = emojiSpans(':salute@$_hash:', const {}, _style);
    expect((spans.single as TextSpan).text, ':salute@$_hash:');
  });

  group('emojiOnlyCount', () {
    test('counts a lone unicode emoji', () {
      expect(emojiOnlyCount('🎉', const {}), 1);
    });

    test('counts several emoji with whitespace between them', () {
      expect(emojiOnlyCount(' 🔥 🔥  🎉 ', const {}), 3);
    });

    test('handles ZWJ sequences, skin tones, flags and keycaps as units', () {
      expect(emojiOnlyCount('👩‍🚒', const {}), 1);
      expect(emojiOnlyCount('👍🏼', const {}), 1);
      expect(emojiOnlyCount('🇳🇴', const {}), 1);
      expect(emojiOnlyCount('1️⃣', const {}), 1);
    });

    test('mixed text is not emoji-only', () {
      expect(emojiOnlyCount('nice 🎉', const {}), isNull);
      expect(emojiOnlyCount('🎉!', const {}), isNull);
    });

    test('plain text and digits are not emoji', () {
      expect(emojiOnlyCount('hello', const {}), isNull);
      expect(emojiOnlyCount('123', const {}), isNull);
      expect(emojiOnlyCount('', const {}), isNull);
    });

    test('over the jumbo limit renders normal again', () {
      expect(emojiOnlyCount('🔥' * jumboEmojiMaxCount, const {}), jumboEmojiMaxCount);
      expect(emojiOnlyCount('🔥' * (jumboEmojiMaxCount + 1), const {}), isNull);
    });

    test('a resolved custom token counts; an unresolved one does not', () {
      final library = {_hash: _emoji('salute')};
      expect(emojiOnlyCount(':salute@$_hash:', library), 1);
      expect(emojiOnlyCount(':salute: 🎉', library), 2);
      expect(emojiOnlyCount(':nope:', library), isNull);
    });
  });

  test('emojiSpans honors a custom emoji size', () {
    final spans = emojiSpans(':salute@$_hash:', {_hash: _emoji('salute')}, _style,
        emojiSize: jumboEmojiSize);
    expect(spans.single, isA<WidgetSpan>());
  });
}
