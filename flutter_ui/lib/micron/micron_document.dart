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

enum MicronFieldKind { text, checkbox, radio }

/// One `` `<...> `` input field. Text fields carry [initial] and [width];
/// checkboxes and radios carry the [value] they submit when selected.
class MicronField {
  const MicronField({
    required this.name,
    required this.kind,
    this.value = '',
    this.initial = '',
    this.width = 24,
    this.masked = false,
    this.preChecked = false,
  });

  final String name;
  final MicronFieldKind kind;
  final String value;
  final String initial;
  final int width;
  final bool masked;
  final bool preChecked;
}

/// One styled run of text, optionally acting as a link or an input field.
class MicronSegment {
  const MicronSegment(this.text, this.style, {this.linkUrl, this.linkFields,
      this.field});

  final String text;
  final MicronStyle style;

  /// Non-null for `[label`url]` links. The URL is raw micron form
  /// (hash:/page/x.mu, :/page/x.mu, /page/x.mu, #anchor ...); resolution
  /// against the current node happens at tap time.
  final String? linkUrl;

  /// The link's third piece: field names to submit, `*` for all of them, and
  /// `name=value` request variables. Null when the link carries none.
  final List<String>? linkFields;

  /// Non-null for `` `<...> `` input fields.
  final MicronField? field;
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

/// A `` `t `` block: rows of cells, each cell a run of styled segments.
class MicronTableLine extends MicronLine {
  const MicronTableLine(this.rows, this.aligns, this.depth);
  final List<List<List<MicronSegment>>> rows;
  final List<MicronAlign> aligns;
  final int depth;
}

class MicronLiteralLine extends MicronLine {
  const MicronLiteralLine(this.text);
  final String text;
}

class MicronDocument {
  const MicronDocument(this.lines,
      {this.anchors = const {}, this.headingLines = const []});

  final List<MicronLine> lines;

  /// Anchor name to the index in [lines] it marks. Explicit `` `: `` anchors
  /// and heading slugs share one namespace; the first declared wins.
  final Map<String, int> anchors;

  /// Indices of heading lines, ascending -- what a bare `#` link jumps to.
  final List<int> headingLines;
}
