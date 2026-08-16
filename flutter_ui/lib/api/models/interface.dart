/// One row of GET /reticulum/interfaces: the configured section merged with
/// live stats when the interface is up.
class RetInterface {
  const RetInterface({
    required this.name,
    required this.type,
    required this.enabled,
    required this.editable,
    this.status,
    this.rxb,
    this.txb,
  });

  final String name;
  final String type;
  final bool enabled;

  /// False for types the config editor doesn't support -- shown read-only.
  final bool editable;

  /// Live up/down from rns.get_interface_stats(); null when RNS has no
  /// stats for it (e.g. disabled or not yet brought up).
  final bool? status;
  final int? rxb;
  final int? txb;

  factory RetInterface.fromJson(Map<String, dynamic> json) => RetInterface(
        name: json['name'] as String,
        type: json['type'] as String? ?? 'Unknown',
        enabled: json['enabled'] as bool? ?? true,
        editable: json['editable'] as bool? ?? false,
        status: json['status'] as bool?,
        rxb: (json['rxb'] as num?)?.toInt(),
        txb: (json['txb'] as num?)?.toInt(),
      );
}
