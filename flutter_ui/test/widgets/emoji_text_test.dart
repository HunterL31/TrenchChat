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
}
