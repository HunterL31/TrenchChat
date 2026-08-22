// Micron markup parser, ported from Nomad Network's MicronParser.py so
// pages written for real NomadNet nodes render the same here.
//
// Total by construction: every character either matches a tag or is emitted
// as text, malformed or truncated tags are dropped, and nothing throws --
// the degenerate result of hostile input is a plain-text document (the
// decodeThemeCode precedent). Formatting state persists across lines, as
// upstream's does, until a reset tag or end of document.
//
// Deliberately not implemented, degrading to plain content: `t tables
// (marker line skipped, rows render as text) and `{ partials (nomadnet
// server-side includes; the line is skipped).
import 'dart:ui' show Color;

import 'micron_document.dart';

MicronDocument parseMicron(String source) {
  final state = _ParserState();
  final lines = <MicronLine>[];
  for (final raw in source.split('\n')) {
    final parsed = _parseLine(raw, state);
    if (parsed != null) lines.add(parsed);
  }
  return MicronDocument(lines);
}

class _ParserState {
  bool literal = false;
  int depth = 0;
  MicronAlign align = MicronAlign.defaultAlign;
  MicronStyle style = MicronStyle.plain;
}

MicronLine? _parseLine(String line, _ParserState state) {
  if (line == '`=') {
    state.literal = !state.literal;
    return null;
  }
  if (state.literal) {
    // Upstream unescapes the one sequence that could end literal mode.
    return MicronLiteralLine(line == r'\`=' ? '`=' : line);
  }
  if (line.isEmpty) {
    return MicronTextLine(const [], state.align, state.depth);
  }

  var preEscape = false;
  var first = line[0];

  // Lines carrying input fields cannot be headings; upstream strips the
  // markers before anything else.
  if (first == '>' && line.contains('`<')) {
    line = line.replaceFirst(RegExp(r'^>+'), '');
    if (line.isEmpty) return MicronTextLine(const [], state.align, state.depth);
    first = line[0];
  }

  if (first == r'\') {
    line = line.substring(1);
    preEscape = true;
  } else if (first == '#') {
    return null;
  }

  if (!preEscape) {
    if (line.startsWith('`t') || line.startsWith('`{')) {
      return null;
    }
    if (first == '<') {
      state.depth = 0;
      return _parseLine(line.substring(1), state);
    }
    if (first == '>') {
      var level = 0;
      while (level < line.length && line[level] == '>') {
        level++;
      }
      state.depth = level;
      final rest = line.substring(level);
      if (rest.isEmpty) return null;
      final segments = _inline(rest, state, pre: false);
      return MicronHeadingLine(segments, level, state.align);
    }
    if (first == '-') {
      var fill = '─';
      if (line.length == 2) {
        final char = line[1];
        if (char.codeUnitAt(0) >= 32) fill = char;
      }
      return MicronDividerLine(fill, state.depth);
    }
  }

  final segments = _inline(line, state, pre: preEscape);
  return MicronTextLine(segments, state.align, state.depth);
}

List<MicronSegment> _inline(String line, _ParserState state,
    {required bool pre}) {
  final output = <MicronSegment>[];
  final part = StringBuffer();
  var escape = pre;
  var formatting = false;
  var i = 0;

  void flush() {
    if (part.isNotEmpty) {
      output.add(MicronSegment(part.toString(), state.style));
      part.clear();
    }
  }

  while (i < line.length) {
    final c = line[i];
    if (formatting) {
      switch (c) {
        case '_':
          state.style = state.style.copyWith(underline: !state.style.underline);
        case '!':
          state.style = state.style.copyWith(bold: !state.style.bold);
        case '*':
          state.style = state.style.copyWith(italic: !state.style.italic);
        case 'F':
          i += _readColor(line, i + 1, (color) {
            state.style = state.style.copyWith(fg: () => color);
          });
        case 'f':
          state.style = state.style.copyWith(fg: () => null);
        case 'B':
          i += _readColor(line, i + 1, (color) {
            state.style = state.style.copyWith(bg: () => color);
          });
        case 'b':
          state.style = state.style.copyWith(bg: () => null);
        case '`':
          state.style = MicronStyle.plain;
          state.align = MicronAlign.defaultAlign;
        case 'c':
          state.align = MicronAlign.center;
        case 'l':
          state.align = MicronAlign.left;
        case 'r':
          state.align = MicronAlign.right;
        case 'a':
          state.align = MicronAlign.defaultAlign;
        case ':':
          // Anchor declaration: consume the name, render nothing.
          var end = i + 1;
          while (end < line.length && _isAnchorChar(line[end])) {
            end++;
          }
          i = end - 1;
        case '[':
          final end = line.indexOf(']', i);
          if (end != -1) {
            final linkData = line.substring(i + 1, end);
            i = end;
            final pieces = linkData.split('`');
            String label;
            String url;
            if (pieces.length == 1) {
              url = pieces[0];
              label = '';
            } else if (pieces.length >= 2 && pieces.length <= 3) {
              // A third piece carries form fields; ignored until forms land.
              label = pieces[0];
              url = pieces[1];
            } else {
              url = '';
              label = '';
            }
            if (url.isNotEmpty) {
              output.add(MicronSegment(label.isEmpty ? url : label, state.style,
                  linkUrl: url));
            }
          }
        case '<':
          // Input field: `<flags|name`data> -- inert placeholder here.
          final backtick = line.indexOf('`', i + 1);
          final end = backtick == -1 ? -1 : line.indexOf('>', backtick);
          if (backtick != -1 && end != -1) {
            final data = line.substring(backtick + 1, end);
            output.add(MicronSegment(
              '⟦${data.isEmpty ? ' ' : data}⟧',
              state.style,
              isField: true,
            ));
            i = end;
          }
        default:
          // Unknown tag: dropped, matching upstream's forward tolerance.
          break;
      }
      formatting = false;
    } else {
      if (c == r'\') {
        if (escape) {
          part.write(c);
          escape = false;
        } else {
          escape = true;
        }
      } else if (c == '`') {
        if (escape) {
          part.write(c);
          escape = false;
        } else {
          formatting = true;
          flush();
        }
      } else {
        part.write(c);
        escape = false;
      }
    }
    i++;
  }
  flush();
  return output;
}

bool _isAnchorChar(String c) =>
    RegExp(r'[A-Za-z0-9_-]').hasMatch(c);

/// Reads a micron color spec starting at [start]: "abc" (3-hex), "gNN"
/// (grayscale 0-99), or "Taabbcc" (truecolor). Calls [apply] when valid and
/// returns how many characters were consumed (0 when malformed/truncated).
int _readColor(String line, int start, void Function(Color) apply) {
  if (start + 3 <= line.length && line[start] == 'T') {
    if (start + 7 <= line.length) {
      final color = _parseHex6(line.substring(start + 1, start + 7));
      if (color != null) apply(color);
      return 7;
    }
    return 0;
  }
  if (start + 3 > line.length) return 0;
  final spec = line.substring(start, start + 3);
  final color = _parseColor3(spec);
  if (color != null) apply(color);
  return 3;
}

Color? _parseColor3(String spec) {
  if (spec.startsWith('g')) {
    final value = int.tryParse(spec.substring(1));
    if (value == null || value < 0 || value > 99) return null;
    final level = (value * 255 / 99).round();
    return Color(0xFF000000 | (level << 16) | (level << 8) | level);
  }
  final digits = spec.split('');
  final values = digits.map((d) => int.tryParse(d, radix: 16)).toList();
  if (values.any((v) => v == null)) return null;
  // CSS-shorthand nibble duplication, matching upstream's high_color().
  final r = values[0]! * 17, g = values[1]! * 17, b = values[2]! * 17;
  return Color(0xFF000000 | (r << 16) | (g << 8) | b);
}

Color? _parseHex6(String spec) {
  final value = int.tryParse(spec, radix: 16);
  if (value == null || spec.length != 6) return null;
  return Color(0xFF000000 | value);
}
