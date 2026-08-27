// Every propagation node heard on the mesh, with the same USE control the
// settings dialog shows for the nearest few. Up to MAX_TRACKED_NODES of them
// are held at once, which is far more than a settings pane should carry.
import 'package:flutter/material.dart';

import '../../api/models/dm.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';

String shortNodeHash(String hex) =>
    hex.length <= 12 ? hex : '${hex.substring(0, 6)}…${hex.substring(hex.length - 6)}';

Future<void> showPropagationNodesDialog(BuildContext context, AppState state) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _PropagationNodesContent(state: state),
    ),
  );
}

class _PropagationNodesContent extends StatefulWidget {
  const _PropagationNodesContent({required this.state});

  final AppState state;

  @override
  State<_PropagationNodesContent> createState() => _PropagationNodesContentState();
}

class _PropagationNodesContentState extends State<_PropagationNodesContent> {
  @override
  void initState() {
    super.initState();
    widget.state.addListener(_onStateChanged);
  }

  @override
  void dispose() {
    widget.state.removeListener(_onStateChanged);
    super.dispose();
  }

  void _onStateChanged() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final propagation = widget.state.propagation;
    final nodes = propagation.nodes;
    return TcDialogShell(
      title: 'Propagation nodes',
      width: 460,
      actions: [
        TcGhostButton(label: 'CLOSE', onPressed: () => Navigator.pop(context)),
      ],
      children: [
        Text(
          nodes.isEmpty
              ? 'No node heard yet.'
              : '${nodes.length} node${nodes.length == 1 ? "" : "s"} heard, '
                  'nearest first.',
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textSecondary),
        ),
        const SizedBox(height: 8),
        // The list is bounded but still long enough to overflow a dialog, so
        // it scrolls inside its own box rather than growing one.
        ConstrainedBox(
          constraints: const BoxConstraints(maxHeight: 320),
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                for (final node in nodes)
                  PropagationNodeRow(state: widget.state, node: node),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

/// One node: how far away it is, and the control to send through it. Shared
/// with the settings pane so both lists behave the same.
class PropagationNodeRow extends StatelessWidget {
  const PropagationNodeRow({super.key, required this.state, required this.node});

  final AppState state;
  final PropagationNode node;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final pinned = state.propagation.pinned == node.hash;
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        children: [
          Expanded(
            child: Text(
              '${shortNodeHash(node.hash)} — ${node.hops} hop'
              '${node.hops == 1 ? "" : "s"}${pinned ? " (pinned)" : ""}',
              style: TextStyle(fontSize: TCType.textBodySm, color: tc.textSecondary),
            ),
          ),
          if (!pinned)
            TcGhostButton(
              label: 'USE',
              onPressed: () => state.pinPropagationNode(node.hash),
            ),
        ],
      ),
    );
  }
}
