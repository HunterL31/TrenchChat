/// Snapshot of GET /settings -- the propagation-node settings the Qt
/// SettingsDialog edits, minus display_name/avatar which have their own
/// endpoints.
class TcSettings {
  const TcSettings({
    required this.propagationEnabled,
    required this.propagationNodeName,
    required this.propagationStorageLimitMb,
    required this.outboundPropagationNode,
  });

  final bool propagationEnabled;
  final String propagationNodeName;
  final int propagationStorageLimitMb;
  final String? outboundPropagationNode;

  factory TcSettings.fromJson(Map<String, dynamic> json) => TcSettings(
        propagationEnabled: json['propagation_enabled'] as bool? ?? false,
        propagationNodeName: json['propagation_node_name'] as String? ?? '',
        propagationStorageLimitMb: json['propagation_storage_limit_mb'] as int? ?? 500,
        outboundPropagationNode: json['outbound_propagation_node'] as String?,
      );

  Map<String, dynamic> toJson() => {
        'propagation_enabled': propagationEnabled,
        'propagation_node_name': propagationNodeName,
        'propagation_storage_limit_mb': propagationStorageLimitMb,
        'outbound_propagation_node': outboundPropagationNode ?? '',
      };
}
