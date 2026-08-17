/// Snapshot of GET /settings -- the propagation-node settings the Qt
/// SettingsDialog edits, minus display_name/avatar which have their own
/// endpoints.
class TcSettings {
  const TcSettings({
    required this.propagationEnabled,
    required this.propagationNodeName,
    required this.propagationStorageLimitMb,
    required this.channelFilterMode,
    required this.channelFilterHashes,
    required this.outboundPropagationNode,
  });

  final bool propagationEnabled;
  final String propagationNodeName;
  final int propagationStorageLimitMb;

  /// `"allowlist"` or `"all"`.
  final String channelFilterMode;
  final List<String> channelFilterHashes;
  final String? outboundPropagationNode;

  factory TcSettings.fromJson(Map<String, dynamic> json) => TcSettings(
        propagationEnabled: json['propagation_enabled'] as bool? ?? false,
        propagationNodeName: json['propagation_node_name'] as String? ?? '',
        propagationStorageLimitMb: json['propagation_storage_limit_mb'] as int? ?? 500,
        channelFilterMode: json['channel_filter_mode'] as String? ?? 'allowlist',
        channelFilterHashes:
            (json['channel_filter_hashes'] as List<dynamic>? ?? []).cast<String>(),
        outboundPropagationNode: json['outbound_propagation_node'] as String?,
      );

  Map<String, dynamic> toJson() => {
        'propagation_enabled': propagationEnabled,
        'propagation_node_name': propagationNodeName,
        'propagation_storage_limit_mb': propagationStorageLimitMb,
        'channel_filter_mode': channelFilterMode,
        'channel_filter_hashes': channelFilterHashes,
        'outbound_propagation_node': outboundPropagationNode ?? '',
      };
}
