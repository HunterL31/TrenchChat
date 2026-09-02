/// GET /reticulum/config: one entry per node-wide Reticulum option -- the
/// [reticulum] and [logging] sections -- carrying its schema (so the editor
/// need not hardcode the option set) plus the value currently in the config
/// file. An empty [value] means the key is unset and RNS uses [defaultValue].
class ReticulumOption {
  const ReticulumOption({
    required this.key,
    required this.section,
    required this.category,
    required this.label,
    required this.kind,
    required this.defaultValue,
    required this.description,
    required this.value,
    this.choices = const [],
  });

  final String key;

  /// 'reticulum' or 'logging'.
  final String section;

  /// UI grouping label, e.g. 'Transport & routing'.
  final String category;
  final String label;

  /// 'bool', 'int', 'float', 'str', 'choice', 'hex' or 'hash_list'.
  final String kind;
  final List<String> choices;

  /// What RNS uses when the key is absent, as display text.
  final String defaultValue;

  /// Tooltip: what the option does and what changing it costs.
  final String description;

  /// The raw value in the config file, or '' when unset.
  final String value;

  bool get isChoice => kind == 'choice';
  bool get isBool => kind == 'bool';

  factory ReticulumOption.fromJson(Map<String, dynamic> json) => ReticulumOption(
        key: json['key'] as String? ?? '',
        section: json['section'] as String? ?? 'reticulum',
        category: json['category'] as String? ?? '',
        label: json['label'] as String? ?? json['key'] as String? ?? '',
        kind: json['kind'] as String? ?? 'str',
        choices: (json['choices'] as List<dynamic>? ?? [])
            .map((e) => e.toString())
            .toList(),
        defaultValue: json['default'] as String? ?? '',
        description: json['description'] as String? ?? '',
        value: json['value'] as String? ?? '',
      );
}
