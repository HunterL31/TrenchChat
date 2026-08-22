// Renders a parsed micron document as Flutter widgets: heading bands,
// character-fill dividers, literal lines verbatim, and styled text with
// tappable links. Colors come from the page's own micron tags where given,
// falling back to the enclosing SectionTheme.
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
  /// :/page/x.mu, ...). Null renders links as plain styled text.
  final void Function(String url)? onLinkTap;

  @override
  State<MicronView> createState() => _MicronViewState();
}

class _MicronViewState extends State<MicronView> {
  late MicronDocument _doc;
  final List<TapGestureRecognizer> _recognizers = [];

  @override
  void initState() {
    super.initState();
    _doc = parseMicron(widget.source);
  }

  @override
  void didUpdateWidget(MicronView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.source != widget.source) {
      _doc = parseMicron(widget.source);
    }
  }

  @override
  void dispose() {
    _disposeRecognizers();
    super.dispose();
  }

  void _disposeRecognizers() {
    for (final recognizer in _recognizers) {
      recognizer.dispose();
    }
    _recognizers.clear();
  }

  static const double _sectionIndent = 16;
  static const double _lineHeight = 1.45;

  @override
  Widget build(BuildContext context) {
    // Spans are rebuilt below; recognizers from the previous frame's spans
    // are no longer reachable and must not leak.
    _disposeRecognizers();
    final tc = SectionTheme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [for (final line in _doc.lines) _lineWidget(tc, line)],
    );
  }

  Widget _lineWidget(TCSectionColors tc, MicronLine line) {
    return switch (line) {
      MicronHeadingLine() => _heading(tc, line),
      MicronDividerLine() => _divider(tc, line),
      MicronLiteralLine() => Text(
          line.text.isEmpty ? ' ' : line.text,
          style: TextStyle(
            fontSize: TCType.textBodySm,
            color: tc.textPrimary,
            height: _lineHeight,
          ),
        ),
      MicronTextLine() => _textLine(tc, line),
    };
  }

  Widget _heading(TCSectionColors tc, MicronHeadingLine line) {
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
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: band,
        border: Border(left: BorderSide(color: tc.borderAccent, width: 2)),
      ),
      child: Text.rich(
        TextSpan(
            children: _spans(tc, line.segments,
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

  Widget _divider(TCSectionColors tc, MicronDividerLine line) {
    return Padding(
      padding: EdgeInsets.only(
        left: _indent(line.depth),
        top: 4,
        bottom: 4,
      ),
      child: LayoutBuilder(
        builder: (context, constraints) {
          const charWidth = 8.0;
          final count =
              (constraints.maxWidth / charWidth).floor().clamp(1, 500);
          return Text(
            line.fillChar * count,
            maxLines: 1,
            overflow: TextOverflow.clip,
            softWrap: false,
            style: TextStyle(
                fontSize: TCType.textBodySm, color: tc.textTertiary),
          );
        },
      ),
    );
  }

  Widget _textLine(TCSectionColors tc, MicronTextLine line) {
    if (line.segments.isEmpty) {
      return Text(' ',
          style: TextStyle(fontSize: TCType.textBodySm, height: _lineHeight));
    }
    final base = TextStyle(
      fontSize: TCType.textBodySm,
      color: tc.textPrimary,
      height: _lineHeight,
    );
    return Padding(
      padding: EdgeInsets.only(left: _indent(line.depth)),
      child: Text.rich(
        TextSpan(children: _spans(tc, line.segments, base: base)),
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

  List<InlineSpan> _spans(TCSectionColors tc, List<MicronSegment> segments,
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
      if (segment.isField) {
        spans.add(TextSpan(
          text: segment.text,
          style: style.copyWith(
            color: tc.textTertiary,
            backgroundColor: tc.bgInset,
          ),
        ));
        continue;
      }
      final url = segment.linkUrl;
      if (url != null && widget.onLinkTap != null) {
        final recognizer = TapGestureRecognizer()
          ..onTap = () => widget.onLinkTap!(url);
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
      spans.add(TextSpan(text: segment.text, style: style));
    }
    return spans;
  }
}
