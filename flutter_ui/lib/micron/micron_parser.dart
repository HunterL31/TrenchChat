// Micron markup parser, ported from Nomad Network's MicronParser.py so
// pages written for real NomadNet nodes render the same here.
//
// Total by construction: every character either matches a tag or is emitted
// as text, malformed or truncated tags are dropped, and nothing throws --
// the degenerate result of hostile input is a plain-text document (the
// decodeThemeCode precedent). Formatting state persists across lines, as
// upstream's does, until a reset tag or end of document.
import 'dart:ui' show Color;

import 'micron_document.dart';

MicronDocument parseMicron(String source) {
  final state = _ParserState();
  final lines = <MicronLine>[];
  final anchors = <String, int>{};
  final headingLines = <int>[];
  for (final raw in source.split('\n')) {
    final parsed = _parseLine(raw, state);
    if (parsed == null) continue;
    final index = lines.length;
    for (final name in state.pendingAnchors) {
      anchors.putIfAbsent(name, () => index);
    }
    state.pendingAnchors.clear();
    if (parsed is MicronHeadingLine) headingLines.add(index);
    lines.add(parsed);
  }
  final trailing = _closeTable(state);
  if (trailing != null) lines.add(trailing);
  return MicronDocument(
    lines,
    anchors: anchors,
    headingLines: headingLines,
    foreground: _pageColor(source, '#!fg='),
    background: _pageColor(source, '#!bg='),
  );
}

/// The background a page declares for itself, for an embedder that paints
/// the space around the document as well as behind it.
Color? micronPageBackground(String source) => _pageColor(source, '#!bg=');

/// A page-wide colour header, wherever in the document it appears. Micron
/// takes the first one and accepts either a 3- or a 6-digit spec.
Color? _pageColor(String source, String header) {
  final start = source.indexOf(header);
  if (start < 0) return null;
  var end = source.indexOf('\n', start);
  if (end < 0) end = source.length;
  final spec = source.substring(start + header.length, end).trim();
  if (spec.length == 3) return _parseColor3(spec);
  if (spec.length == 6) return _parseHex6(spec);
  return null;
}

/// Bidirectional overrides and isolates let a page reorder what a reader
/// sees without changing the text, so a link label can be made to read as
/// something else entirely. Nomadnet strips a wider set (zero-width joiners
/// included, which breaks emoji); these are the characters that actually
/// mislead.
final RegExp _displaySpoofing =
    RegExp(r'[\u202A-\u202E\u2066-\u2069\uFEFF]');

String _safeText(String text) => text.replaceAll(_displaySpoofing, '');

/// Micron's own heading-to-anchor rule: strip tags, then collapse every run
/// of non-alphanumerics into a single hyphen and lowercase the result.
String slugifyMicron(String text) {
  final stripped = text.replaceAll(_tagPattern, '');
  return stripped
      .replaceAll(RegExp(r'[^A-Za-z0-9]+'), '-')
      .replaceAll(RegExp(r'^-+|-+$'), '')
      .toLowerCase();
}

final RegExp _tagPattern = RegExp(r'`[FB]T[0-9a-fA-F]{6}'
    r'|`[FB][0-9a-fA-F]{3}'
    r'|`:[A-Za-z0-9_\-]*'
    r'|`[!*_=fbacrl`<>{]');

class _ParserState {
  bool literal = false;
  bool table = false;
  final List<String> tableBuffer = [];
  MicronAlign tableAlign = MicronAlign.defaultAlign;
  int? tableMaxWidth;
  int depth = 0;
  MicronAlign align = MicronAlign.defaultAlign;
  MicronStyle style = MicronStyle.plain;
  final List<String> pendingAnchors = [];
}

MicronLine? _parseLine(String line, _ParserState state) {
  if (line == '`=') {
    state.literal = !state.literal;
    return null;
  }
  if (state.literal) {
    // Upstream unescapes the one sequence that could end literal mode.
    return MicronLiteralLine(_safeText(line == r'\`=' ? '`=' : line));
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
    if (line.startsWith('`t')) {
      if (state.table) return _closeTable(state);
      state.table = true;
      state.tableBuffer.clear();
      _readTableArgs(line.substring(2), state);
      return null;
    }
    if (state.table) {
      state.tableBuffer.add(line);
      return null;
    }
    if (line.startsWith('`{')) {
      return _parsePartial(line.substring(2), state);
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
      final slug = slugifyMicron(rest);
      if (slug.isNotEmpty) state.pendingAnchors.add(slug);
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
      output.add(MicronSegment(_safeText(part.toString()), state.style));
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
          var end = i + 1;
          while (end < line.length && _isAnchorChar(line[end])) {
            end++;
          }
          final name = line.substring(i + 1, end);
          if (name.isNotEmpty) state.pendingAnchors.add(name);
          i = end - 1;
        case '[':
          final end = line.indexOf(']', i);
          if (end != -1) {
            final linkData = line.substring(i + 1, end);
            i = end;
            final pieces = linkData.split('`');
            String label;
            String url;
            List<String>? fields;
            if (pieces.length == 1) {
              url = pieces[0];
              label = '';
            } else if (pieces.length >= 2 && pieces.length <= 3) {
              label = pieces[0];
              url = pieces[1];
              if (pieces.length == 3 && pieces[2].isNotEmpty) {
                fields = pieces[2].split('|');
              }
            } else {
              url = '';
              label = '';
            }
            if (url.isNotEmpty) {
              output.add(MicronSegment(label.isEmpty ? url : label, state.style,
                  linkUrl: url, linkFields: fields));
            }
          }
        case '<':
          final field = _readField(line, i);
          if (field != null) {
            output.add(MicronSegment('', state.style, field: field.field));
            i = field.end;
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

/// Ends the open table block and renders what it buffered. Null when the
/// block held nothing a table can be made of, which drops it silently as
/// upstream does.
MicronTableLine? _closeTable(_ParserState state) {
  if (!state.table) return null;
  state.table = false;
  final align = state.tableAlign;
  final maxWidth = state.tableMaxWidth;
  state.tableAlign = MicronAlign.defaultAlign;
  state.tableMaxWidth = null;
  final raw = List<String>.from(state.tableBuffer);
  state.tableBuffer.clear();

  final rows = <List<String>>[];
  for (final line in raw) {
    final trimmed = line.trim();
    if (trimmed.isEmpty) continue;
    var cells = trimmed.split('|');
    if (cells.isNotEmpty && cells.first.trim().isEmpty) cells = cells.sublist(1);
    if (cells.isNotEmpty && cells.last.trim().isEmpty) {
      cells = cells.sublist(0, cells.length - 1);
    }
    if (cells.isEmpty) continue;
    rows.add(cells.map((c) => c.trim()).toList());
  }
  if (rows.isEmpty) return null;

  var aligns = <MicronAlign>[];
  if (rows.length > 1 && rows[1].every(_isRuleCell)) {
    aligns = rows[1].map(_ruleAlign).toList();
    rows.removeAt(1);
  }

  final parsed = [
    for (final row in rows)
      [for (final cell in row) _inline(cell, state, pre: false)]
  ];
  return MicronTableLine(parsed, aligns, state.depth,
      align: align, maxWidth: maxWidth);
}

/// Reads the block tag's own arguments: an alignment letter and a maximum
/// width in characters, as in `` `tc60 ``. Malformed arguments are dropped.
void _readTableArgs(String args, _ParserState state) {
  state.tableAlign = MicronAlign.defaultAlign;
  state.tableMaxWidth = null;
  if (args.isEmpty) return;
  switch (args[0]) {
    case 'l':
      state.tableAlign = MicronAlign.left;
      args = args.substring(1);
    case 'c':
      state.tableAlign = MicronAlign.center;
      args = args.substring(1);
    case 'r':
      state.tableAlign = MicronAlign.right;
      args = args.substring(1);
  }
  final width = int.tryParse(args.trim());
  if (width != null && width > 0) state.tableMaxWidth = width;
}

/// Reads `` `{url`refresh`fields} `` starting after the opening tag. Null
/// when the tag is truncated or names no URL, which drops the line.
MicronPartialLine? _parsePartial(String rest, _ParserState state) {
  final end = rest.indexOf('}');
  if (end == -1) return null;
  final pieces = rest.substring(0, end).split('`');
  final url = pieces[0].trim();
  if (url.isEmpty) return null;

  double? refresh;
  if (pieces.length >= 2) {
    refresh = double.tryParse(pieces[1].trim());
    // Micron's own floor: a sub-second interval means "do not refresh".
    if (refresh != null && refresh < 1) refresh = null;
  }

  final fields = <String>[];
  String? id;
  if (pieces.length >= 3 && pieces[2].isNotEmpty) {
    for (final field in pieces[2].split('|')) {
      if (field.isEmpty) continue;
      // pid= names the partial for `p:` links and is also submitted, as
      // upstream submits it -- a node-side page can see which one asked.
      if (field.startsWith('pid=')) id = field.substring(4);
      fields.add(field);
    }
  }
  return MicronPartialLine(
      url: url,
      depth: state.depth,
      id: id,
      refreshSecs: refresh,
      fields: fields);
}

bool _isRuleCell(String cell) =>
    cell.isNotEmpty && RegExp(r'^:?-{2,}:?$').hasMatch(cell);

MicronAlign _ruleAlign(String cell) {
  final left = cell.startsWith(':');
  final right = cell.endsWith(':');
  if (left && right) return MicronAlign.center;
  if (right) return MicronAlign.right;
  return MicronAlign.left;
}

class _ParsedField {
  const _ParsedField(this.field, this.end);
  final MicronField field;
  final int end;
}

/// Reads `` `<flags|name|value|*`data> `` starting at the `<`. Returns null
/// when the tag is truncated or has no name, which drops it as upstream does.
_ParsedField? _readField(String line, int start) {
  final backtick = line.indexOf('`', start + 1);
  if (backtick == -1) return null;
  final end = line.indexOf('>', backtick);
  if (end == -1) return null;

  final head = line.substring(start + 1, backtick);
  final data = line.substring(backtick + 1, end);

  var kind = MicronFieldKind.text;
  var name = head;
  var value = '';
  var width = 24;
  var masked = false;
  var preChecked = false;

  if (head.contains('|')) {
    final parts = head.split('|');
    var flags = parts[0];
    name = parts.length > 1 ? parts[1] : '';
    if (flags.contains('^')) {
      kind = MicronFieldKind.radio;
      flags = flags.replaceAll('^', '');
    } else if (flags.contains('?')) {
      kind = MicronFieldKind.checkbox;
      flags = flags.replaceAll('?', '');
    } else if (flags.contains('!')) {
      flags = flags.replaceAll('!', '');
      masked = true;
    }
    if (flags.isNotEmpty) {
      final parsed = int.tryParse(flags);
      if (parsed != null) width = parsed.clamp(1, 256);
    }
    if (parts.length > 2) value = parts[2];
    if (parts.length > 3 && parts[3] == '*') preChecked = true;
  }

  if (name.isEmpty) return null;
  final isToggle = kind != MicronFieldKind.text;
  return _ParsedField(
    MicronField(
      name: name,
      kind: kind,
      value: isToggle ? (value.isNotEmpty ? value : data) : '',
      initial: isToggle ? '' : data,
      width: width,
      masked: masked,
      preChecked: preChecked,
    ),
    end,
  );
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
