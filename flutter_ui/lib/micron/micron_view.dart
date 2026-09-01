// Renders a parsed micron document as Flutter widgets: heading bands,
// character-fill dividers, tables, literal lines verbatim, live input
// fields, and styled text with tappable links. Colors come from the page's
// own micron tags where given, falling back to the enclosing SectionTheme.
//
// Anchors are the view's own business: a `#name` link scrolls this document
// rather than fetching anything, so it never reaches [onLinkTap].
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import '../theme/section_theme.dart';
import '../theme/theme_spec.dart';
import '../theme/tokens.dart';
import 'micron_document.dart';
import 'micron_parser.dart';

class MicronView extends StatefulWidget {
  const MicronView({super.key, required this.source, this.onLinkTap});

  final String source;

  /// Called with the raw micron URL of a tapped link (hash:/page/x.mu,
  /// :/page/x.mu, ...) and the request data its field list collected, empty
  /// when it carries none. Null renders links as plain styled text.
  final void Function(String url, Map<String, String> data)? onLinkTap;

  @override
  State<MicronView> createState() => _MicronViewState();
}

class _MicronViewState extends State<MicronView> {
  late MicronDocument _doc;
  final List<TapGestureRecognizer> _recognizers = [];
  final Map<int, GlobalKey> _lineKeys = {};

  final Map<String, TextEditingController> _textFields = {};
  final Map<String, Set<String>> _checkboxes = {};
  final Map<String, String> _radios = {};

  @override
  void initState() {
    super.initState();
    _load(widget.source);
  }

  @override
  void didUpdateWidget(MicronView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.source != widget.source) {
      _load(widget.source);
    }
  }

  @override
  void dispose() {
    _disposeRecognizers();
    _disposeFields();
    super.dispose();
  }

  void _load(String source) {
    _disposeFields();
    _lineKeys.clear();
    _doc = parseMicron(source);
    for (final index in [..._doc.anchors.values, ..._doc.headingLines]) {
      _lineKeys.putIfAbsent(index, () => GlobalKey());
    }
    for (final line in _doc.lines) {
      for (final segment in _segmentsOf(line)) {
        final field = segment.field;
        if (field == null) continue;
        switch (field.kind) {
          case MicronFieldKind.text:
            _textFields[field.name] =
                TextEditingController(text: field.initial);
          case MicronFieldKind.checkbox:
            final checked = _checkboxes.putIfAbsent(field.name, () => {});
            if (field.preChecked) checked.add(field.value);
          case MicronFieldKind.radio:
            if (field.preChecked) _radios[field.name] = field.value;
        }
      }
    }
  }

  void _disposeFields() {
    for (final controller in _textFields.values) {
      controller.dispose();
    }
    _textFields.clear();
    _checkboxes.clear();
    _radios.clear();
  }

  void _disposeRecognizers() {
    for (final recognizer in _recognizers) {
      recognizer.dispose();
    }
    _recognizers.clear();
  }

  Iterable<MicronSegment> _segmentsOf(MicronLine line) => switch (line) {
        MicronTextLine() => line.segments,
        MicronHeadingLine() => line.segments,
        MicronTableLine() => line.rows.expand((row) => row.expand((c) => c)),
        MicronDividerLine() || MicronLiteralLine() => const [],
      };

  static const double _sectionIndent = 16;
  static const double _lineHeight = 1.45;
  static const double _fallbackCharWidth = 8;

  /// Width of one character in the body style. Dividers fill the line with a
  /// repeated glyph and fields are sized in characters, so both need the
  /// real advance rather than a guess.
  double _charWidth(BuildContext context, TextStyle style) {
    final painter = TextPainter(
      text: TextSpan(text: '0', style: _resolved(context, style)),
      textDirection: TextDirection.ltr,
    )..layout();
    final width = painter.width;
    painter.dispose();
    return width > 0 ? width : _fallbackCharWidth;
  }

  TextStyle _resolved(BuildContext context, TextStyle style) =>
      DefaultTextStyle.of(context).style.merge(style);

  @override
  Widget build(BuildContext context) {
    // Spans are rebuilt below; recognizers from the previous frame's spans
    // are no longer reachable and must not leak.
    _disposeRecognizers();
    final tc = SectionTheme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (var i = 0; i < _doc.lines.length; i++)
          _lineWidget(tc, _doc.lines[i], i),
      ],
    );
  }

  Widget _lineWidget(TCSectionColors tc, MicronLine line, int index) {
    final key = _lineKeys[index];
    return switch (line) {
      MicronHeadingLine() => _heading(tc, line, index, key),
      MicronDividerLine() => _divider(tc, line, key),
      MicronTableLine() => _table(tc, line, index, key),
      MicronLiteralLine() => Text(
          line.text.isEmpty ? ' ' : line.text,
          key: key,
          style: TextStyle(
            fontSize: TCType.textBodySm,
            color: tc.textPrimary,
            height: _lineHeight,
          ),
        ),
      MicronTextLine() => _textLine(tc, line, index, key),
    };
  }

  Widget _heading(
      TCSectionColors tc, MicronHeadingLine line, int index, Key? key) {
    final level = line.level.clamp(1, 3);
    final fontSize = switch (level) {
      1 => TCType.textBodyLg + 4,
      2 => TCType.textBodyLg + 1,
      _ => TCType.textBodyMd,
    };
    // Like nomadnet's inverted heading rows: a band that fades with depth.
    final band = switch (level) {
      1 => tc.bgSelected,
      2 => tc.bgInset,
      _ => tc.bgSurface,
    };
    return Container(
      key: key,
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: band,
        border: Border(left: BorderSide(color: tc.borderAccent, width: 2)),
      ),
      child: Text.rich(
        TextSpan(
            children: _spans(tc, line.segments, index,
                base: TextStyle(
                  fontSize: fontSize,
                  fontWeight: FontWeight.w600,
                  color: tc.textEmphasis,
                  height: 1.3,
                ))),
        textAlign: _textAlign(line.align),
      ),
    );
  }

  Widget _divider(TCSectionColors tc, MicronDividerLine line, Key? key) {
    final style = TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary);
    return Padding(
      key: key,
      padding: EdgeInsets.only(
        left: _indent(line.depth),
        top: 4,
        bottom: 4,
      ),
      child: LayoutBuilder(
        builder: (context, constraints) {
          final charWidth = _charWidth(context, style);
          final count =
              (constraints.maxWidth / charWidth).floor().clamp(1, 500);
          return Text(
            line.fillChar * count,
            maxLines: 1,
            overflow: TextOverflow.clip,
            softWrap: false,
            style: style,
          );
        },
      ),
    );
  }

  Widget _table(
      TCSectionColors tc, MicronTableLine line, int index, Key? key) {
    final base = TextStyle(
      fontSize: TCType.textBodySm,
      color: tc.textPrimary,
      height: _lineHeight,
    );
    return Padding(
      key: key,
      padding: EdgeInsets.only(left: _indent(line.depth), top: 4, bottom: 4),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Table(
          defaultColumnWidth: const IntrinsicColumnWidth(),
          border: TableBorder.all(color: tc.borderSubtle),
          children: [
            for (final row in line.rows)
              TableRow(
                children: [
                  for (var c = 0; c < row.length; c++)
                    Padding(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 8, vertical: 3),
                      child: Text.rich(
                        TextSpan(children: _spans(tc, row[c], index, base: base)),
                        textAlign: _textAlign(c < line.aligns.length
                            ? line.aligns[c]
                            : MicronAlign.left),
                      ),
                    ),
                ],
              ),
          ],
        ),
      ),
    );
  }

  Widget _textLine(
      TCSectionColors tc, MicronTextLine line, int index, Key? key) {
    if (line.segments.isEmpty) {
      return Text(' ',
          key: key,
          style: TextStyle(fontSize: TCType.textBodySm, height: _lineHeight));
    }
    final base = TextStyle(
      fontSize: TCType.textBodySm,
      color: tc.textPrimary,
      height: _lineHeight,
    );
    return Padding(
      key: key,
      padding: EdgeInsets.only(left: _indent(line.depth)),
      child: Text.rich(
        TextSpan(children: _spans(tc, line.segments, index, base: base)),
        textAlign: _textAlign(line.align),
      ),
    );
  }

  double _indent(int depth) => depth > 1 ? (depth - 1) * _sectionIndent : 0;

  TextAlign _textAlign(MicronAlign align) => switch (align) {
        MicronAlign.center => TextAlign.center,
        MicronAlign.right => TextAlign.right,
        MicronAlign.left || MicronAlign.defaultAlign => TextAlign.left,
      };

  List<InlineSpan> _spans(
      TCSectionColors tc, List<MicronSegment> segments, int lineIndex,
      {required TextStyle base}) {
    final spans = <InlineSpan>[];
    for (final segment in segments) {
      var style = base.copyWith(
        fontWeight: segment.style.bold ? FontWeight.w700 : null,
        fontStyle: segment.style.italic ? FontStyle.italic : null,
        decoration: segment.style.underline ? TextDecoration.underline : null,
        color: segment.style.fg ?? base.color,
        backgroundColor: segment.style.bg,
      );
      final field = segment.field;
      if (field != null) {
        spans.add(WidgetSpan(
          alignment: PlaceholderAlignment.middle,
          child: SelectionContainer.disabled(
              child: _fieldWidget(tc, field, style)),
        ));
        continue;
      }
      final url = segment.linkUrl;
      if (url != null && widget.onLinkTap != null) {
        final recognizer = TapGestureRecognizer()
          ..onTap = () => _onLink(url, segment.linkFields, lineIndex);
        _recognizers.add(recognizer);
        spans.add(TextSpan(
          text: segment.text,
          recognizer: recognizer,
          mouseCursor: SystemMouseCursors.click,
          style: style.copyWith(
            color: segment.style.fg ?? tc.linkColor,
            decoration: TextDecoration.underline,
          ),
        ));
        continue;
      }
      if (url != null) {
        style = style.copyWith(
          color: segment.style.fg ?? tc.linkColor,
          decoration: TextDecoration.underline,
        );
      }
      spans.add(TextSpan(text: _paintable(segment), style: style));
    }
    return spans;
  }

  /// Micron draws bars and blocks as background-coloured spaces, and Flutter
  /// paints no background behind whitespace it trims at the end of a line.
  /// Non-breaking spaces keep the run and its colour.
  String _paintable(MicronSegment segment) =>
      segment.style.bg == null
          ? segment.text
          : segment.text.replaceAll(' ', '\u00A0');

  Widget _fieldWidget(
      TCSectionColors tc, MicronField field, TextStyle style) {
    final fill = tc.bgInset;
    switch (field.kind) {
      case MicronFieldKind.text:
        final controller = _textFields[field.name];
        return Container(
          width: field.width * _charWidth(context, style) + 10,
          padding: const EdgeInsets.symmetric(horizontal: 4),
          decoration: BoxDecoration(
            color: fill,
            border: Border.all(color: tc.borderDefault),
          ),
          child: TextField(
            controller: controller,
            obscureText: field.masked,
            style: style.copyWith(backgroundColor: null, color: tc.textPrimary),
            cursorColor: tc.borderAccent,
            decoration: const InputDecoration(
              isDense: true,
              contentPadding: EdgeInsets.symmetric(vertical: 4),
              border: InputBorder.none,
            ),
          ),
        );
      case MicronFieldKind.checkbox:
        final checked =
            _checkboxes[field.name]?.contains(field.value) ?? false;
        return _toggle(tc, style, fill, checked ? '[x]' : '[ ]', () {
          setState(() {
            final set = _checkboxes.putIfAbsent(field.name, () => {});
            if (checked) {
              set.remove(field.value);
            } else {
              set.add(field.value);
            }
          });
        });
      case MicronFieldKind.radio:
        final selected = _radios[field.name] == field.value;
        return _toggle(tc, style, fill, selected ? '(o)' : '( )', () {
          setState(() => _radios[field.name] = field.value);
        });
    }
  }

  Widget _toggle(TCSectionColors tc, TextStyle style, Color fill, String glyph,
      VoidCallback onTap) {
    return GestureDetector(
      onTap: onTap,
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        child: Container(
          color: fill,
          padding: const EdgeInsets.symmetric(horizontal: 2),
          child: Text(glyph,
              style: style.copyWith(
                  backgroundColor: null, color: tc.textEmphasis)),
        ),
      ),
    );
  }

  void _onLink(String url, List<String>? fields, int lineIndex) {
    if (url.startsWith('#')) {
      _jumpToAnchor(url.substring(1), lineIndex);
      return;
    }
    widget.onLinkTap?.call(url, _requestData(fields));
  }

  /// Scrolls to a named anchor, or -- for a bare `#` -- to the next heading
  /// after the link that was tapped.
  void _jumpToAnchor(String name, int lineIndex) {
    int? target;
    if (name.isEmpty) {
      for (final index in _doc.headingLines) {
        if (index > lineIndex) {
          target = index;
          break;
        }
      }
    } else {
      target = _doc.anchors[name];
    }
    if (target == null) return;
    final key = _lineKeys[target];
    final anchorContext = key?.currentContext;
    if (anchorContext == null) return;
    Scrollable.ensureVisible(anchorContext,
        duration: const Duration(milliseconds: 250), alignment: 0.05);
  }

  Map<String, String> _requestData(List<String>? fields) {
    if (fields == null || fields.isEmpty) return const {};
    final data = <String, String>{};
    final wanted = <String>{};
    var all = false;
    for (final entry in fields) {
      if (entry == '*') {
        all = true;
      } else if (entry.contains('=')) {
        final parts = entry.split('=');
        if (parts.length == 2 && parts[0].isNotEmpty) {
          data['var_${parts[0]}'] = parts[1];
        }
      } else if (entry.isNotEmpty) {
        wanted.add(entry);
      }
    }
    bool include(String name) => all || wanted.contains(name);
    for (final entry in _textFields.entries) {
      if (include(entry.key)) data['field_${entry.key}'] = entry.value.text;
    }
    for (final entry in _checkboxes.entries) {
      if (include(entry.key) && entry.value.isNotEmpty) {
        data['field_${entry.key}'] = entry.value.join(',');
      }
    }
    for (final entry in _radios.entries) {
      if (include(entry.key)) data['field_${entry.key}'] = entry.value;
    }
    return data;
  }
}
