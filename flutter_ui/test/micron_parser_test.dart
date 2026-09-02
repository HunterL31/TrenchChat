import 'dart:math';
import 'dart:ui' show Color;

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/micron/micron_document.dart';
import 'package:flutter_ui/micron/micron_parser.dart';

String _textOf(MicronLine line) => switch (line) {
      MicronTextLine(:final segments) => segments.map((s) => s.text).join(),
      MicronHeadingLine(:final segments) => segments.map((s) => s.text).join(),
      MicronLiteralLine(:final text) => text,
      MicronTableLine(:final rows) =>
        rows.map((r) => r.map((c) => c.map((s) => s.text).join()).join('|'))
            .join('\n'),
      MicronDividerLine() => '',
      MicronPartialLine(:final url) => url,
    };

MicronField _fieldOf(MicronLine line) =>
    (line as MicronTextLine).segments.singleWhere((s) => s.field != null).field!;

void main() {
  group('line forms', () {
    test('plain text parses as one segment', () {
      final doc = parseMicron('hello world');
      final line = doc.lines.single as MicronTextLine;
      expect(line.segments.single.text, 'hello world');
      expect(line.segments.single.style.bold, isFalse);
    });

    test('comments vanish', () {
      final doc = parseMicron('# a comment\nvisible');
      expect(doc.lines, hasLength(1));
      expect(_textOf(doc.lines.single), 'visible');
    });

    test('headings carry their level and set section depth', () {
      final doc = parseMicron('>Title\ntext\n>>Sub\ndeeper');
      final h1 = doc.lines[0] as MicronHeadingLine;
      expect(h1.level, 1);
      expect(_textOf(h1), 'Title');
      expect((doc.lines[1] as MicronTextLine).depth, 1);
      final h2 = doc.lines[2] as MicronHeadingLine;
      expect(h2.level, 2);
      expect((doc.lines[3] as MicronTextLine).depth, 2);
    });

    test('a < line resets section depth', () {
      final doc = parseMicron('>>Deep\n<back at top');
      final line = doc.lines[1] as MicronTextLine;
      expect(line.depth, 0);
      expect(_textOf(line), 'back at top');
    });

    test('dividers pick up a custom fill char, rejecting control chars', () {
      expect((parseMicron('-').lines.single as MicronDividerLine).fillChar,
          '─');
      expect((parseMicron('-~').lines.single as MicronDividerLine).fillChar,
          '~');
      expect((parseMicron('-\x07').lines.single as MicronDividerLine).fillChar,
          '─');
    });

    test('literal mode passes tags through verbatim', () {
      final doc = parseMicron('`=\n`!not bold `[not`a link]\n# not a comment\n`=\nafter');
      expect(doc.lines[0], isA<MicronLiteralLine>());
      expect(_textOf(doc.lines[0]), '`!not bold `[not`a link]');
      expect(_textOf(doc.lines[1]), '# not a comment');
      expect(doc.lines[2], isA<MicronTextLine>());
      expect(_textOf(doc.lines[2]), 'after');
    });

    test(r'escaped \`= inside literal mode unescapes', () {
      final doc = parseMicron('`=\n\\`=\n`=');
      expect(_textOf(doc.lines.single), '`=');
    });

    test('a backslash-escaped line renders its first char as text', () {
      final doc = parseMicron('\\>not a heading');
      expect(doc.lines.single, isA<MicronTextLine>());
      expect(_textOf(doc.lines.single), '>not a heading');
    });

    test('partial lines become a line of their own', () {
      final doc = parseMicron('`{part`5}');
      final partial = doc.lines.single as MicronPartialLine;
      expect(partial.url, 'part');
      expect(partial.refreshSecs, 5);
    });
  });

  group('inline formatting', () {
    test('bold toggles on and off', () {
      final doc = parseMicron('a`!b`!c');
      final segments = (doc.lines.single as MicronTextLine).segments;
      expect(segments.map((s) => s.text).toList(), ['a', 'b', 'c']);
      expect(segments[0].style.bold, isFalse);
      expect(segments[1].style.bold, isTrue);
      expect(segments[2].style.bold, isFalse);
    });

    test('style state persists across lines until reset', () {
      final doc = parseMicron('`!bold\nstill bold\n``\nplain');
      expect((doc.lines[0] as MicronTextLine).segments.single.style.bold,
          isTrue);
      expect((doc.lines[1] as MicronTextLine).segments.single.style.bold,
          isTrue);
      expect((doc.lines[3] as MicronTextLine).segments.single.style.bold,
          isFalse);
    });

    test('3-hex colors expand by nibble duplication', () {
      final doc = parseMicron('`F0a2colored');
      final style = (doc.lines.single as MicronTextLine).segments.single.style;
      expect(style.fg, const Color(0xFF00AA22));
    });

    test('truecolor and grayscale color forms parse', () {
      final tcDoc = parseMicron('`FT0a141ex');
      expect((tcDoc.lines.single as MicronTextLine).segments.single.style.fg,
          const Color(0xFF0A141E));
      final grayDoc = parseMicron('`Fg99white');
      expect(
          (grayDoc.lines.single as MicronTextLine).segments.single.style.fg,
          const Color(0xFFFFFFFF));
    });

    test('f and b reset the colors', () {
      final doc = parseMicron('`F123`B456x`f`by');
      final segments = (doc.lines.single as MicronTextLine).segments;
      expect(segments[0].style.fg, isNotNull);
      expect(segments[0].style.bg, isNotNull);
      expect(segments[1].style.fg, isNull);
      expect(segments[1].style.bg, isNull);
    });

    test('alignment tags set the line alignment', () {
      final doc = parseMicron('`ccentered\n`aback');
      expect((doc.lines[0] as MicronTextLine).align, MicronAlign.center);
      expect((doc.lines[1] as MicronTextLine).align, MicronAlign.defaultAlign);
    });

    test('escaped backtick is literal text', () {
      final doc = parseMicron('a \\` b');
      expect(_textOf(doc.lines.single), 'a ` b');
    });

    test('anchors are consumed invisibly', () {
      final doc = parseMicron('`:section-2 visible');
      expect(_textOf(doc.lines.single), ' visible');
    });
  });

  group('links', () {
    test('label`url form', () {
      final doc = parseMicron('see `[the docs`abcd:/page/docs.mu] ok');
      final segments = (doc.lines.single as MicronTextLine).segments;
      final link = segments.singleWhere((s) => s.linkUrl != null);
      expect(link.text, 'the docs');
      expect(link.linkUrl, 'abcd:/page/docs.mu');
      expect(_textOf(doc.lines.single), 'see the docs ok');
    });

    test('bare url form uses the url as its label', () {
      final doc = parseMicron('`[/page/index.mu]');
      final link =
          (doc.lines.single as MicronTextLine).segments.single;
      expect(link.text, '/page/index.mu');
      expect(link.linkUrl, '/page/index.mu');
    });

    test('link with a fields part keeps label and url', () {
      final doc = parseMicron('`[submit`/page/act.mu`field_a|field_b]');
      final link =
          (doc.lines.single as MicronTextLine).segments.single;
      expect(link.text, 'submit');
      expect(link.linkUrl, '/page/act.mu');
    });

    test('an unterminated link tag drops the tag, keeps the text', () {
      // Upstream behavior: `[` with no `]` is discarded and scanning
      // continues in text mode.
      final doc = parseMicron('before `[broken');
      expect(_textOf(doc.lines.single), 'before broken');
    });
  });

  group('fields', () {
    test('a text field carries its name, width and starting text', () {
      final field = _fieldOf(parseMicron('Name: `<24|username`guest>').lines.single);
      expect(field.kind, MicronFieldKind.text);
      expect(field.name, 'username');
      expect(field.width, 24);
      expect(field.initial, 'guest');
      expect(field.masked, isFalse);
    });

    test('a bare field name is the whole spec', () {
      final field = _fieldOf(parseMicron('`<user_input`Pre-defined data>').lines.single);
      expect(field.name, 'user_input');
      expect(field.initial, 'Pre-defined data');
    });

    test('the ! flag masks the field', () {
      final field = _fieldOf(parseMicron('`<!32|secret`hunter2>').lines.single);
      expect(field.masked, isTrue);
      expect(field.width, 32);
    });

    test('a checkbox carries the value it submits', () {
      final field = _fieldOf(parseMicron('`<?|sign_up|1`>').lines.single);
      expect(field.kind, MicronFieldKind.checkbox);
      expect(field.name, 'sign_up');
      expect(field.value, '1');
      expect(field.preChecked, isFalse);
    });

    test('a trailing * pre-checks a toggle', () {
      final field = _fieldOf(parseMicron('`<?|box|1|*`>').lines.single);
      expect(field.preChecked, isTrue);
    });

    test('a radio option is its own field with a value', () {
      final field = _fieldOf(parseMicron('`<^|color|Red`>').lines.single);
      expect(field.kind, MicronFieldKind.radio);
      expect(field.name, 'color');
      expect(field.value, 'Red');
    });

    test('a field with no name is dropped', () {
      final doc = parseMicron('`<|`x>');
      expect((doc.lines.single as MicronTextLine).segments
          .where((s) => s.field != null), isEmpty);
    });

    test('a heading line containing a field is demoted to text', () {
      final doc = parseMicron('>Title `<f`v>');
      expect(doc.lines.single, isA<MicronTextLine>());
    });
  });

  group('links', () {
    test('a third piece is the field list to submit', () {
      final doc = parseMicron('`[Send`:/page/e.mu`user|action=view]');
      final link = (doc.lines.single as MicronTextLine).segments.single;
      expect(link.linkUrl, ':/page/e.mu');
      expect(link.linkFields, ['user', 'action=view']);
    });

    test('a link with no field list carries none', () {
      final doc = parseMicron('`[Go`:/page/e.mu]');
      expect((doc.lines.single as MicronTextLine).segments.single.linkFields,
          isNull);
    });

    test('an anchor link keeps its # url', () {
      final doc = parseMicron('`[Jump`#notes]');
      expect((doc.lines.single as MicronTextLine).segments.single.linkUrl,
          '#notes');
    });
  });

  group('anchors', () {
    test('a heading declares its slug', () {
      final doc = parseMicron('intro\n>Introduction & Setup\nbody');
      expect(doc.anchors['introduction-setup'], 1);
      expect(doc.headingLines, [1]);
    });

    test('an explicit anchor marks the line it sits on', () {
      final doc = parseMicron('one\n`:install-notes here\ntwo');
      expect(doc.anchors['install-notes'], 1);
    });

    test('the first declaration of a name wins', () {
      final doc = parseMicron('>Notes\n`:notes later');
      expect(doc.anchors['notes'], 0);
    });
  });

  group('tables', () {
    test('a `t block becomes a table with alignments', () {
      final doc = parseMicron(
          '`t\n| Name | Price |\n| ---- | ----: |\n| `!Apple`! | Free |\n`t');
      final table = doc.lines.single as MicronTableLine;
      expect(table.rows.length, 2);
      expect(_textOf(table), 'Name|Price\nApple|Free');
      expect(table.aligns, [MicronAlign.left, MicronAlign.right]);
    });

    test('an unterminated table still renders its rows', () {
      final doc = parseMicron('`t\n| a | b |');
      expect(doc.lines.single, isA<MicronTableLine>());
    });

    test('the block tag carries its own alignment and width', () {
      final doc = parseMicron('`tc60\n| a | b |\n`t');
      final table = doc.lines.single as MicronTableLine;
      expect(table.align, MicronAlign.center);
      expect(table.maxWidth, 60);
    });

    test('a bare `t block has neither', () {
      final doc = parseMicron('`t\n| a | b |\n`t');
      final table = doc.lines.single as MicronTableLine;
      expect(table.align, MicronAlign.defaultAlign);
      expect(table.maxWidth, isNull);
    });

    test('a width with no alignment letter still reads', () {
      final table = parseMicron('`t30\n| a |\n`t').lines.single
          as MicronTableLine;
      expect(table.align, MicronAlign.defaultAlign);
      expect(table.maxWidth, 30);
    });

    test('nonsense arguments are dropped, not applied', () {
      final table = parseMicron('`txyz\n| a |\n`t').lines.single
          as MicronTableLine;
      expect(table.align, MicronAlign.defaultAlign);
      expect(table.maxWidth, isNull);
    });

    test('a second table does not inherit the first one\'s arguments', () {
      final doc = parseMicron('`tr40\n| a |\n`t\n`t\n| b |\n`t');
      final second = doc.lines[1] as MicronTableLine;
      expect(second.align, MicronAlign.defaultAlign);
      expect(second.maxWidth, isNull);
    });
  });

  group('page colours', () {
    test('#!fg= and #!bg= headers set the page colours', () {
      final doc = parseMicron('#!bg=222\n#!fg=ddd\nhello');
      expect(doc.background, const Color(0xFF222222));
      expect(doc.foreground, const Color(0xFFDDDDDD));
    });

    test('a six-digit spec is read as truecolor', () {
      final doc = parseMicron('#!bg=102030\nhello');
      expect(doc.background, const Color(0xFF102030));
    });

    test('a page with no headers has no colours of its own', () {
      final doc = parseMicron('hello');
      expect(doc.background, isNull);
      expect(doc.foreground, isNull);
    });

    test('a malformed spec is ignored', () {
      final doc = parseMicron('#!bg=nonsense\n#!fg=12\nhello');
      expect(doc.background, isNull);
      expect(doc.foreground, isNull);
    });

    test('the header line itself is still a comment', () {
      final doc = parseMicron('#!bg=222\nhello');
      expect(doc.lines, hasLength(1));
      expect(_textOf(doc.lines.single), 'hello');
    });
  });

  group('partials', () {
    test('a bare partial names only its url', () {
      final doc = parseMicron('`{:/page/side.mu}');
      final partial = doc.lines.single as MicronPartialLine;
      expect(partial.url, ':/page/side.mu');
      expect(partial.refreshSecs, isNull);
      expect(partial.id, isNull);
      expect(partial.fields, isEmpty);
    });

    test('a partial carries its refresh interval and fields', () {
      final doc = parseMicron('`{:/page/side.mu`5`pid=side|name|mode=live}');
      final partial = doc.lines.single as MicronPartialLine;
      expect(partial.refreshSecs, 5);
      expect(partial.id, 'side');
      expect(partial.fields, ['pid=side', 'name', 'mode=live']);
    });

    test('pid is submitted as well as naming the partial', () {
      // Upstream leaves pid= in the field list, so a node-side page can see
      // which partial asked; dropping it would send the node less.
      final doc = parseMicron('`{:/page/side.mu`0`pid=side}');
      final partial = doc.lines.single as MicronPartialLine;
      expect(partial.id, 'side');
      expect(partial.fields, ['pid=side']);
    });

    test('a sub-second interval means do not refresh', () {
      final doc = parseMicron('`{:/page/side.mu`0.2}');
      expect((doc.lines.single as MicronPartialLine).refreshSecs, isNull);
    });

    test('a truncated or empty partial is dropped', () {
      expect(parseMicron('`{:/page/side.mu').lines, isEmpty);
      expect(parseMicron('`{}').lines, isEmpty);
    });

    test('a partial takes the section depth it sits in', () {
      final doc = parseMicron('>>Section\n`{:/page/side.mu}');
      expect((doc.lines[1] as MicronPartialLine).depth, 2);
    });
  });

  group('display safety', () {
    test('bidi overrides are stripped from text', () {
      // U+202E would reverse everything after it, so a link label could be
      // made to read as a different destination than the one it names.
      final doc = parseMicron('safe \u202Ereversed\u202C tail');
      expect(_textOf(doc.lines.single), 'safe reversed tail');
    });

    test('bidi overrides are stripped from literal blocks too', () {
      final doc = parseMicron('`=\nart \u202Ehere\n`=');
      expect(_textOf(doc.lines.single), 'art here');
    });

    test('zero-width joiners survive, so emoji sequences still render', () {
      final doc = parseMicron('a\u200Db');
      expect(_textOf(doc.lines.single), 'a\u200Db');
    });
  });

  group('totality', () {
    test('truncated tags at end of line are dropped silently', () {
      for (final source in ['`', '`F', '`F1', '`FT12345', '`<', '`<a`', '`[']) {
        expect(() => parseMicron(source), returnsNormally,
            reason: 'input: $source');
      }
    });

    test('random garbage never throws', () {
      final random = Random(1234);
      const alphabet = '`[]<>|!_*FfBbclra:\\-=#\ntext ⟦x⟧ ';
      for (var round = 0; round < 200; round++) {
        final source = List.generate(300,
            (_) => alphabet[random.nextInt(alphabet.length)]).join();
        expect(() => parseMicron(source), returnsNormally);
      }
    });

    test('a large pathological page parses', () {
      final source = ('`!`_`*`F123`B456`[x`/page/y.mu]' * 2000);
      expect(() => parseMicron(source), returnsNormally);
    });
  });
}
