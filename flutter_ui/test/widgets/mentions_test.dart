// The text carries the identity's hash, never a name, so rendering is a
// lookup: the reader sees whoever that identity is to them now. A hash the
// reader cannot name renders short rather than as a name nothing can vouch
// for.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/mentions.dart';
import 'package:flutter_ui/widgets/emoji_text.dart';

const _alice = 'aa11bb22cc33dd44ee55ff6677889900';
const _bob = 'cc11bb22cc33dd44ee55ff6677889900';
const _style = TextStyle(fontSize: 14);

final _names = {_alice: 'Alice', _bob: 'Bob'};

MentionConfig _config({String? me}) => MentionConfig(
      style: const TextStyle(color: Color(0xFF00FF00)),
      selfStyle: const TextStyle(color: Color(0xFFFF0000)),
      resolveName: (hash) => _names[hash],
      selfHash: me,
    );

void main() {
  group('reading mentions out of text', () {
    test('a token names the identity it holds', () {
      expect(mentionsIn('hi @$_alice'), [_alice]);
    });

    test('the same identity twice is one mention', () {
      expect(mentionsIn('@$_alice @$_alice'), [_alice]);
    });

    test('a longer hex run names somebody else', () {
      expect(mentionsIn('@${_alice}ffffffff'), isEmpty);
    });

    test('upper case names the same identity', () {
      expect(mentionsIn('@${_alice.toUpperCase()}'), [_alice]);
    });

    test('contentMentions is exact, not a substring search', () {
      expect(contentMentions('@$_alice', _alice), isTrue);
      expect(contentMentions('@${_alice}ffffffff', _alice), isFalse);
      expect(contentMentions('@$_bob', _alice), isFalse);
      expect(contentMentions('@$_alice', null), isFalse);
    });
  });

  group('rendering mentions', () {
    test('a mention renders as the name this client knows', () {
      final spans = emojiSpans('hi @$_alice there', const {}, _style,
          mentions: _config());
      expect(spans, hasLength(3));
      expect((spans[0] as TextSpan).text, 'hi ');
      expect((spans[1] as TextSpan).text, '@Alice');
      expect((spans[2] as TextSpan).text, ' there');
    });

    test('an identity with no known name renders short, never invented', () {
      final spans = emojiSpans('@${'ff' * 16}', const {}, _style,
          mentions: _config());
      expect((spans.single as TextSpan).text, '@ffffffff…');
    });

    test('a mention of the reader is styled apart from the rest', () {
      final spans = emojiSpans('@$_alice and @$_bob', const {}, _style,
          mentions: _config(me: _alice));
      final mine = spans[0] as TextSpan;
      final theirs = spans[2] as TextSpan;
      expect(mine.text, '@Alice');
      expect(mine.style!.color, const Color(0xFFFF0000));
      expect(theirs.text, '@Bob');
      expect(theirs.style!.color, const Color(0xFF00FF00));
    });

    test('the run style survives the mention style', () {
      const jumbo = TextStyle(fontSize: 34);
      final spans =
          emojiSpans('@$_alice', const {}, jumbo, mentions: _config());
      expect((spans.single as TextSpan).style!.fontSize, 34);
    });

    test('without a config a mention is left as written', () {
      final spans = emojiSpans('hi @$_alice', const {}, _style);
      expect((spans.single as TextSpan).text, 'hi @$_alice');
    });
  });

  group('offering candidates', () {
    const roster = [
      MentionCandidate(identityHash: _alice, displayName: 'Alice'),
      MentionCandidate(identityHash: _bob, displayName: 'Bobalice'),
    ];

    test('an empty query offers everyone', () {
      expect(matchMentionCandidates(roster, '').length, 2);
    });

    test('a name the query starts comes before one that merely contains it', () {
      final matches = matchMentionCandidates(roster, 'alice');
      expect(matches.first.identityHash, _alice);
      expect(matches.last.identityHash, _bob);
    });

    test('a query matching nobody offers nobody', () {
      expect(matchMentionCandidates(roster, 'zebra'), isEmpty);
    });

    test('a hash prefix finds a peer whose name is not known', () {
      const unnamed = [MentionCandidate(identityHash: _bob, displayName: '')];
      expect(matchMentionCandidates(unnamed, 'cc11').single.identityHash, _bob);
    });

    test('the offer is capped', () {
      final many = [
        for (var i = 0; i < 20; i++)
          MentionCandidate(identityHash: '$i'.padLeft(32, '0'), displayName: 'p$i'),
      ];
      expect(matchMentionCandidates(many, '', limit: 6), hasLength(6));
    });
  });
}
