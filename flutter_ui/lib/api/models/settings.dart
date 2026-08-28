/// Snapshot of GET /settings -- the propagation-node settings the Qt
/// SettingsDialog edits, minus display_name/avatar which have their own
/// endpoints.
class TcSettings {
  const TcSettings({
    required this.propagationEnabled,
    required this.propagationNodeName,
    required this.propagationStorageLimitMb,
    this.outboundPropagationNode = '',
  });

  final bool propagationEnabled;
  final String propagationNodeName;
  final int propagationStorageLimitMb;

  /// The node this client hands offline direct messages to, as a hex
  /// destination hash; empty means it picks one from what the mesh announces.
  /// Read-only here: POST /propagation/node is what changes it, because that
  /// sets the live router as well as the stored setting.
  final String outboundPropagationNode;

  factory TcSettings.fromJson(Map<String, dynamic> json) => TcSettings(
        propagationEnabled: json['propagation_enabled'] as bool? ?? false,
        propagationNodeName: json['propagation_node_name'] as String? ?? '',
        propagationStorageLimitMb: json['propagation_storage_limit_mb'] as int? ?? 500,
        outboundPropagationNode: json['outbound_propagation_node'] as String? ?? '',
      );

  Map<String, dynamic> toJson() => {
        'propagation_enabled': propagationEnabled,
        'propagation_node_name': propagationNodeName,
        'propagation_storage_limit_mb': propagationStorageLimitMb,
      };
}
