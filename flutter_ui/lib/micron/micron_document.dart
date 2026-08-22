// Parsed representation of a micron document (Nomad Network's markup).
// Pure data: no widgets, no parsing -- see micron_parser.dart.
import 'dart:ui' show Color;

enum MicronAlign { defaultAlign, left, center, right }

/// Inline character style. Null colors mean "the theme's default".
class MicronStyle {
  const MicronStyle({
    this.bold = false,
    this.underline = false,
    this.italic = false,
    this.fg,
    this.bg,
  });

  final bool bold;
  final bool underline;
  final bool italic;
  final Color? fg;
  final Color? bg;

  static const plain = MicronStyle();

  MicronStyle copyWith({
    bool? bold,
    bool? underline,
    bool? italic,
    Color? Function()? fg,
    Color? Function()? bg,
  }) =>
      MicronStyle(
        bold: bold ?? this.bold,
        underline: underline ?? this.underline,
        italic: italic ?? this.italic,
        fg: fg != null ? fg() : this.fg,
        bg: bg != null ? bg() : this.bg,
      );
}

/// One styled run of text, optionally acting as a link or an inert
/// input-field placeholder.
class MicronSegment {
  const MicronSegment(this.text, this.style, {this.linkUrl, this.isField = false});

  final String text;
  final MicronStyle style;

  /// Non-null for `[label`url]` links. The URL is raw micron form
  /// (hash:/page/x.mu, :/page/x.mu, /page/x.mu ...); resolution against the
  /// current node happens at tap time.
  final String? linkUrl;

  /// True for `<...>` input fields, rendered inert in this client.
  final bool isField;
}

sealed class MicronLine {
  const MicronLine();
}

class MicronTextLine extends MicronLine {
  const MicronTextLine(this.segments, this.align, this.depth);
  final List<MicronSegment> segments;
  final MicronAlign align;

  /// Section depth from the last heading; indents the line.
  final int depth;
}

class MicronHeadingLine extends MicronLine {
  const MicronHeadingLine(this.segments, this.level, this.align);
  final List<MicronSegment> segments;

  /// 1 for ">", 2 for ">>", ... (visual style capped at 3 by the view).
  final int level;
  final MicronAlign align;
}

class MicronDividerLine extends MicronLine {
  const MicronDividerLine(this.fillChar, this.depth);
  final String fillChar;
  final int depth;
}

class MicronLiteralLine extends MicronLine {
  const MicronLiteralLine(this.text);
  final String text;
}

class MicronDocument {
  const MicronDocument(this.lines);
  final List<MicronLine> lines;
}
