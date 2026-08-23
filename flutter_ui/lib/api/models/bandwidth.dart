/// GET /bandwidth: windowed transfer rates over the Reticulum interfaces.
class BandwidthWindow {
  const BandwidthWindow({
    required this.secs,
    required this.rxBytes,
    required this.txBytes,
    this.rxPerSec,
    this.txPerSec,
  });

  /// The window this row covers (10, 60, 300 seconds).
  final int secs;
  final int rxBytes;
  final int txBytes;

  /// Null until two samples span the window (e.g. right after startup).
  final double? rxPerSec;
  final double? txPerSec;

  factory BandwidthWindow.fromJson(Map<String, dynamic> json) => BandwidthWindow(
        secs: (json['secs'] as num).toInt(),
        rxBytes: (json['rx_bytes'] as num?)?.toInt() ?? 0,
        txBytes: (json['tx_bytes'] as num?)?.toInt() ?? 0,
        rxPerSec: (json['rx_per_sec'] as num?)?.toDouble(),
        txPerSec: (json['tx_per_sec'] as num?)?.toDouble(),
      );
}

class BandwidthReport {
  const BandwidthReport({
    required this.totalRx,
    required this.totalTx,
    required this.windows,
  });

  final int totalRx;
  final int totalTx;
  final List<BandwidthWindow> windows;

  factory BandwidthReport.fromJson(Map<String, dynamic> json) {
    final totals = json['totals'] as Map<String, dynamic>? ?? {};
    return BandwidthReport(
      totalRx: (totals['rx'] as num?)?.toInt() ?? 0,
      totalTx: (totals['tx'] as num?)?.toInt() ?? 0,
      windows: (json['windows'] as List<dynamic>? ?? [])
          .map((e) => BandwidthWindow.fromJson(e as Map<String, dynamic>))
          .toList(),
    );
  }
}
