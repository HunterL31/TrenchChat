/// GET /reticulum/discovery: the [reticulum]-section discovery settings plus
/// the entry points the running RNS instance has discovered on the mesh.
class DiscoverySettings {
  const DiscoverySettings({
    required this.discoverInterfaces,
    required this.autoconnectCount,
    this.requiredDiscoveryValue,
  });

  final bool discoverInterfaces;

  /// autoconnect_discovered_interfaces; 0 when auto-connection is off.
  final int autoconnectCount;
  final int? requiredDiscoveryValue;

  factory DiscoverySettings.fromJson(Map<String, dynamic> json) =>
      DiscoverySettings(
        discoverInterfaces: json['discover_interfaces'] as bool? ?? false,
        autoconnectCount:
            (json['autoconnect_discovered_interfaces'] as num?)?.toInt() ?? 0,
        requiredDiscoveryValue:
            (json['required_discovery_value'] as num?)?.toInt(),
      );
}

class DiscoveredInterface {
  const DiscoveredInterface({
    required this.name,
    required this.type,
    required this.status,
    required this.pinnable,
    this.hops,
    this.value,
    this.lastHeard,
    this.reachableOn,
    this.port,
    this.discoveryHash,
  });

  final String name;
  final String type;

  /// "available", "unknown" or "stale" -- RNS's freshness buckets.
  final String status;

  /// True for network entry points the backend can pin into the config.
  final bool pinnable;
  final int? hops;

  /// The announce's proof-of-work stamp value.
  final int? value;

  /// Epoch seconds of the last discovery announce heard.
  final double? lastHeard;
  final String? reachableOn;
  final int? port;
  final String? discoveryHash;

  factory DiscoveredInterface.fromJson(Map<String, dynamic> json) =>
      DiscoveredInterface(
        name: json['name'] as String? ?? '',
        type: json['type'] as String? ?? 'Unknown',
        status: json['status'] as String? ?? 'unknown',
        pinnable: json['pinnable'] as bool? ?? false,
        hops: (json['hops'] as num?)?.toInt(),
        value: (json['value'] as num?)?.toInt(),
        lastHeard: (json['last_heard'] as num?)?.toDouble(),
        reachableOn: json['reachable_on'] as String?,
        port: (json['port'] as num?)?.toInt(),
        discoveryHash: json['discovery_hash'] as String?,
      );
}

class DiscoveryReport {
  const DiscoveryReport({required this.settings, required this.interfaces});

  final DiscoverySettings settings;
  final List<DiscoveredInterface> interfaces;

  factory DiscoveryReport.fromJson(Map<String, dynamic> json) =>
      DiscoveryReport(
        settings: DiscoverySettings.fromJson(
            json['settings'] as Map<String, dynamic>? ?? {}),
        interfaces: (json['interfaces'] as List<dynamic>? ?? [])
            .map((e) => DiscoveredInterface.fromJson(e as Map<String, dynamic>))
            .toList(),
      );
}
