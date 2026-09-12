// The breakdown behind the header's link pill: which members of the channel
// this node has a path to, how far away they are, and where the pill's
// summary came from. Hover on a pointer, tap on a narrow or touch layout.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../api/models/link_quality.dart';
import '../../format.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/signal_meter.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_tooltip.dart';
import 'map_tab.dart';

/// Pointing at the pill for this long opens the panel, so crossing the header
/// on the way somewhere else never does.
const Duration linkPopoverOpenDelay = Duration(milliseconds: 250);

/// Grace after the pointer leaves the pill, so it can travel into the panel.
const Duration _closeGrace = Duration(milliseconds: 150);

const int _maxRows = 6;
const double _panelWidth = 300;

class LinkQualityPopover extends StatefulWidget {
  const LinkQualityPopover({
    super.key,
    required this.quality,
    required this.child,
    this.compact = false,
  });

  final ChannelLinkQuality quality;

  /// The pill itself.
  final Widget child;

  /// Narrow layout: the panel opens on a tap rather than on hover, since the
  /// pill has no label to point at and the pointer may not be a mouse.
  final bool compact;

  @override
  State<LinkQualityPopover> createState() => _LinkQualityPopoverState();
}

class _LinkQualityPopoverState extends State<LinkQualityPopover> {
  final OverlayPortalController _portal = OverlayPortalController();
  final LayerLink _link = LayerLink();
  Timer? _openTimer;
  Timer? _closeTimer;

  /// Opened by a tap rather than by hover: it stays up until dismissed, and
  /// takes a barrier and the Escape key with it.
  bool _pinned = false;

  @override
  void dispose() {
    _openTimer?.cancel();
    _closeTimer?.cancel();
    super.dispose();
  }

  void _scheduleOpen() {
    _closeTimer?.cancel();
    if (_portal.isShowing) return;
    _openTimer ??= Timer(linkPopoverOpenDelay, () {
      _openTimer = null;
      if (mounted) _portal.show();
    });
  }

  void _scheduleClose() {
    _openTimer?.cancel();
    _openTimer = null;
    if (_pinned) return;
    _closeTimer ??= Timer(_closeGrace, () {
      _closeTimer = null;
      if (mounted) _portal.hide();
    });
  }

  void _keepOpen() {
    _closeTimer?.cancel();
    _closeTimer = null;
  }

  void _close() {
    _openTimer?.cancel();
    _openTimer = null;
    _closeTimer?.cancel();
    _closeTimer = null;
    _pinned = false;
    _portal.hide();
  }

  void _toggleTap() {
    if (_portal.isShowing) {
      _close();
    } else {
      setState(() => _pinned = true);
      _portal.show();
    }
  }

  @override
  Widget build(BuildContext context) {
    Widget pill = OverlayPortal(
      controller: _portal,
      overlayChildBuilder: _buildOverlay,
      child: CompositedTransformTarget(link: _link, child: widget.child),
    );

    pill = Semantics(
      label: _semanticsLabel(widget.quality),
      button: widget.compact,
      child: pill,
    );

    if (widget.compact) {
      return GestureDetector(onTap: _toggleTap, child: pill);
    }
    return MouseRegion(
      onEnter: (_) => _scheduleOpen(),
      onExit: (_) => _scheduleClose(),
      child: GestureDetector(onTap: _toggleTap, child: pill),
    );
  }

  Widget _buildOverlay(BuildContext context) {
    Widget panel = MouseRegion(
      onEnter: (_) => _keepOpen(),
      onExit: (_) => _scheduleClose(),
      child: _LinkQualityPanel(quality: widget.quality),
    );
    if (_pinned) {
      panel = CallbackShortcuts(
        bindings: {const SingleActivator(LogicalKeyboardKey.escape): _close},
        child: Focus(autofocus: true, child: panel),
      );
    }
    return Stack(
      children: [
        if (_pinned)
          Positioned.fill(
            child: GestureDetector(
              behavior: HitTestBehavior.translucent,
              onTap: _close,
            ),
          ),
        Positioned(
          left: 0,
          top: 0,
          child: CompositedTransformFollower(
            link: _link,
            targetAnchor: Alignment.bottomRight,
            followerAnchor: Alignment.topRight,
            offset: const Offset(0, 6),
            child: panel,
          ),
        ),
      ],
    );
  }
}

/// The same reading the panel shows, in one sentence, for a screen reader that
/// cannot hover the pill.
String _semanticsLabel(ChannelLinkQuality q) {
  final parts = <String>['${q.reachable} of ${q.total} members reachable'];
  if (q.medianHops != null) parts.add('median ${_hopsLabel(q.medianHops!)}');
  final best = q.bestName;
  if (best != null && q.bestHops != null) {
    parts.add('closest $best at ${_hopsLabel(q.bestHops!)}');
  }
  return 'Channel link quality: ${parts.join(', ')}.';
}

String _hopsLabel(int hops) => '$hops hop${hops == 1 ? '' : 's'}';

class _LinkQualityPanel extends StatelessWidget {
  const _LinkQualityPanel({required this.quality});

  final ChannelLinkQuality quality;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final reachable = quality.reachablePeers.toList();
    final unreachable = quality.unreachablePeers.toList();

    return Material(
      type: MaterialType.transparency,
      child: Container(
        width: _panelWidth,
        padding: const EdgeInsets.fromLTRB(10, 8, 10, 8),
        decoration: tcTooltipDecoration(context),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            _summaryLine(tc),
            const SizedBox(height: 6),
            ..._group(tc, reachable, (p) => _ReachableRow(peer: p)),
            if (reachable.isNotEmpty && unreachable.isNotEmpty)
              const SizedBox(height: 4),
            ..._group(tc, unreachable, (p) => _UnreachableRow(peer: p)),
            if (quality.peers.isEmpty) _muted(tc, 'No other members yet.'),
            const SizedBox(height: 6),
            _muted(
              tc,
              'From the Reticulum path table. Updates on topology change and '
              'every ${linkQualityRefreshInterval.inSeconds} s.',
            ),
          ],
        ),
      ),
    );
  }

  List<Widget> _group(TCSectionColors tc, List<PeerLinkQuality> peers,
      Widget Function(PeerLinkQuality) row) {
    final shown = peers.take(_maxRows).map(row).toList();
    if (peers.length > _maxRows) {
      shown.add(_muted(tc, '+${peers.length - _maxRows} more'));
    }
    return shown;
  }

  Widget _summaryLine(TCSectionColors tc) {
    final parts = <String>['${quality.reachable} of ${quality.total} reachable'];
    if (quality.medianHops != null) {
      parts.add('median ${_hopsLabel(quality.medianHops!)}');
    }
    final best = quality.bestName;
    if (best != null && quality.bestHops != null) {
      parts.add('closest $best (${_hopsLabel(quality.bestHops!)})');
    }
    return Text(
      parts.join(' · '),
      style: TextStyle(fontSize: TCType.textCaption, color: tc.textPrimary),
    );
  }

  Widget _muted(TCSectionColors tc, String text) => Padding(
        padding: const EdgeInsets.only(top: 2),
        child: Text(
          text,
          style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
        ),
      );
}

class _ReachableRow extends StatelessWidget {
  const _ReachableRow({required this.peer});

  final PeerLinkQuality peer;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final details = <String>[
      if (peer.via != null && peer.via!.isNotEmpty) 'via ${mapShortHex(peer.via!)}',
      if (peer.rttMs != null) 'RTT ${peer.rttMs!.toStringAsFixed(0)} ms',
      if (peer.pathExpiresIn != null)
        'path expires in ${formatDuration(peer.pathExpiresIn!)}',
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: 5),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              SignalMeter(level: peer.level, size: 10),
              const SizedBox(width: 6),
              if (peer.isOnline) ...[
                const StatusDot(status: PresenceStatus.online, size: 8),
                const SizedBox(width: 5),
              ],
              Expanded(
                child: Text(
                  peer.displayName,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: TCType.textCaption, color: tc.textPrimary),
                ),
              ),
              const SizedBox(width: 6),
              Text(
                _hopsLabel(peer.hops!),
                style: TextStyle(fontSize: TCType.textMicro, color: tc.textSecondary),
              ),
            ],
          ),
          if (details.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(left: 22, top: 1),
              child: Text(
                details.join(' · '),
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
              ),
            ),
        ],
      ),
    );
  }
}

class _UnreachableRow extends StatelessWidget {
  const _UnreachableRow({required this.peer});

  final PeerLinkQuality peer;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 3),
      child: Text(
        '${peer.displayName} · no path',
        overflow: TextOverflow.ellipsis,
        style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
      ),
    );
  }
}
