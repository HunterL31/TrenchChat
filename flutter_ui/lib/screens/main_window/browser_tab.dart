// NET tab -- a Nomad Network page browser. Discovered nodes and bookmarks
// when idle; fetched micron pages once a node is opened. Fetches run on the
// backend (POST /nomad/browse) and complete via nomad_fetch WS events; the
// page content itself is pulled from the cache endpoint afterwards, so a
// previously seen page renders instantly and refreshes in place.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../api/models/nomad.dart';
import '../../app_state.dart';
import '../../format.dart';
import '../../micron/micron_view.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';
import '../dialogs/nomad_hosting_dialog.dart';

class BrowserTab extends StatefulWidget {
  const BrowserTab({super.key, required this.state});

  final AppState state;

  @override
  State<BrowserTab> createState() => _BrowserTabState();
}

class _BrowserTabState extends State<BrowserTab> {
  final TextEditingController _address = TextEditingController();
  final List<({String nodeHash, String path})> _history = [];
  int _historyIndex = -1;

  String? _activeFetchId;
  String? _fileFetchId;
  NomadPage? _page;
  String? _error;
  String? _info;
  double _progress = 0;
  bool _loading = false;

  ({String nodeHash, String path})? get _current =>
      _historyIndex >= 0 && _historyIndex < _history.length
          ? _history[_historyIndex]
          : null;

  @override
  void initState() {
    super.initState();
    widget.state.addListener(_onStateChanged);
    if (widget.state.nomadNodes.isEmpty) {
      widget.state.loadNomadNodes();
    }
    widget.state.loadNomadBookmarks();
    final pending = widget.state.takePendingNomadUrl();
    if (pending != null) {
      Future.microtask(() => _go(pending));
    }
  }

  @override
  void dispose() {
    widget.state.removeListener(_onStateChanged);
    _address.dispose();
    super.dispose();
  }

  /// Rebuilds on every AppState notification (node list, bookmarks),
  /// advances the in-flight fetches, and opens URLs handed over from other
  /// tabs (a nomad link tapped in chat).
  void _onStateChanged() {
    if (!mounted) return;
    final pending = widget.state.takePendingNomadUrl();
    if (pending != null) {
      Future.microtask(() => _go(pending));
    }
    _advanceFileFetch();
    final fetchId = _activeFetchId;
    final status = fetchId == null ? null : widget.state.nomadFetches[fetchId];
    if (status == null) {
      setState(() {});
      return;
    }
    if (!status.isTerminal) {
      setState(() => _progress = status.progress);
      return;
    }
    widget.state.takeNomadFetch(fetchId!);
    _activeFetchId = null;
    if (status.status == 'done') {
      setState(() {});
      _showCached(status.nodeHash, status.path, doneLoading: true);
    } else {
      setState(() {
        _loading = false;
        _error = _friendlyReason(status.reason);
      });
    }
  }

  Future<void> _showCached(String nodeHash, String path,
      {bool doneLoading = false}) async {
    final page = await widget.state.fetchCachedNomadPage(nodeHash, path);
    if (!mounted) return;
    setState(() {
      if (page != null) _page = page;
      if (doneLoading) {
        _loading = false;
        _error = page == null ? 'Fetched page could not be read back.' : null;
      }
    });
  }

  Future<void> _go(String url) async {
    if (url.trim().isEmpty) return;
    final fetchId = await widget.state
        .browseNomad(url.trim(), currentNode: _current?.nodeHash);
    if (!mounted) return;
    if (fetchId == null) {
      setState(() =>
          _error = widget.state.takeActionError() ?? 'Could not open that URL.');
      return;
    }
    final status = widget.state.nomadFetches[fetchId];
    final nodeHash = status?.nodeHash ?? '';
    final path = status?.path ?? '/page/index.mu';
    // A repeat visit while the same fetch is still in flight reuses its id;
    // only push history when the location actually changes.
    if (_current == null ||
        _current!.nodeHash != nodeHash ||
        _current!.path != path) {
      _history.removeRange(_historyIndex + 1, _history.length);
      _history.add((nodeHash: nodeHash, path: path));
      _historyIndex = _history.length - 1;
    }
    setState(() {
      _activeFetchId = fetchId;
      _loading = true;
      _error = null;
      _info = null;
      _progress = 0;
      _address.text = '$nodeHash:$path';
    });
    // Stale-while-revalidate: show whatever we already have immediately.
    _showCached(nodeHash, path);
  }

  void _navigateHistory(int delta) {
    final target = _historyIndex + delta;
    if (target < 0 || target >= _history.length) return;
    final entry = _history[target];
    setState(() {
      _historyIndex = target;
      _page = null;
      _error = null;
      _activeFetchId = null;
      _loading = false;
      _address.text = '${entry.nodeHash}:${entry.path}';
    });
    _showCached(entry.nodeHash, entry.path);
  }

  void _reload() {
    final current = _current;
    if (current == null) return;
    _go('${current.nodeHash}:${current.path}');
  }

  void _home() {
    setState(() {
      _history.clear();
      _historyIndex = -1;
      _page = null;
      _error = null;
      _info = null;
      _activeFetchId = null;
      _loading = false;
      _address.text = '';
    });
    widget.state.loadNomadNodes();
    widget.state.loadNomadBookmarks();
  }

  Future<void> _toggleBookmark() async {
    final current = _current;
    if (current == null) return;
    final node = widget.state.nomadNodes[current.nodeHash];
    final label = node?.displayName.isNotEmpty == true
        ? node!.displayName
        : current.path;
    await widget.state
        .toggleNomadBookmark(current.nodeHash, current.path, label);
  }

  String _friendlyReason(String? reason) => switch (reason) {
        'unreachable' =>
          'Node unreachable — no path on the mesh right now. Try again later.',
        'timeout' => 'The node did not answer in time.',
        'too_large' => 'The node sent more data than this client accepts.',
        'link_closed' => 'The link to the node dropped mid-transfer.',
        'busy' => 'Too many requests to this node are already in flight.',
        'bad_path' => 'That page path is not valid.',
        'bad_response' => 'The node sent something that is not a page.',
        _ => 'The page could not be fetched.',
      };

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      color: tc.bgApp,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _navBar(tc),
          if (_loading)
            // Determinate even at zero: an indeterminate bar animates
            // forever, which reads as busier than a mesh fetch is.
            LinearProgressIndicator(
              value: _progress,
              minHeight: 2,
              backgroundColor: tc.bgInset,
              color: tc.borderAccent,
            ),
          if (_error != null) _errorBanner(tc),
          if (_info != null) _infoBanner(tc),
          Expanded(
            child: _current == null ? _nodeList(tc) : _pageView(tc),
          ),
        ],
      ),
    );
  }

  Widget _navBar(TCSectionColors tc) {
    final current = _current;
    final bookmarked = current != null &&
        widget.state.isNomadBookmarked(current.nodeHash, current.path);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: tc.borderDefault)),
      ),
      child: Row(
        children: [
          TcGhostButton(
              label: '<',
              onPressed: _historyIndex > 0 ? () => _navigateHistory(-1) : null),
          const SizedBox(width: 4),
          TcGhostButton(
              label: '>',
              onPressed: _historyIndex < _history.length - 1
                  ? () => _navigateHistory(1)
                  : null),
          const SizedBox(width: 4),
          TcGhostButton(
              icon: TcIcons.sync,
              label: 'RELOAD',
              onPressed: current == null ? null : _reload),
          const SizedBox(width: 4),
          TcGhostButton(
              icon: TcIcons.globe,
              label: 'NODES',
              onPressed: current == null ? null : _home),
          const SizedBox(width: 10),
          Expanded(
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              decoration: BoxDecoration(
                color: tc.bgInset,
                border: Border.all(color: tc.borderDefault),
              ),
              child: TextField(
                controller: _address,
                onSubmitted: _go,
                style: TextStyle(
                    fontSize: TCType.textBodySm, color: tc.textPrimary),
                decoration: InputDecoration(
                  isDense: true,
                  border: InputBorder.none,
                  hintText: '<node hash>:/page/index.mu',
                  hintStyle: TextStyle(
                      fontSize: TCType.textBodySm, color: tc.textTertiary),
                ),
              ),
            ),
          ),
          const SizedBox(width: 6),
          TcGhostButton(label: 'GO', onPressed: () => _go(_address.text)),
          const SizedBox(width: 4),
          TcGhostButton(
            label: bookmarked ? '★' : '☆',
            onPressed: current == null ? null : _toggleBookmark,
          ),
          const SizedBox(width: 4),
          TcGhostButton(
            label: 'HOST',
            onPressed: () => showNomadHostingDialog(context, widget.state),
          ),
        ],
      ),
    );
  }

  Widget _errorBanner(TCSectionColors tc) {
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 10, 12, 0),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: tc.bgInset,
        border: Border.all(color: tc.statusDanger),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              _error!,
              style:
                  TextStyle(fontSize: TCType.textCaption, color: tc.statusDanger),
            ),
          ),
          TcGhostButton(label: 'RETRY', onPressed: _reload),
        ],
      ),
    );
  }

  Widget _infoBanner(TCSectionColors tc) {
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 10, 12, 0),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: tc.bgInset,
        border: Border.all(color: tc.borderAccent),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              _info!,
              style: TextStyle(
                  fontSize: TCType.textCaption, color: tc.textSecondary),
            ),
          ),
          TcGhostButton(
              label: 'OK', onPressed: () => setState(() => _info = null)),
        ],
      ),
    );
  }

  Widget _nodeList(TCSectionColors tc) {
    final bookmarks = widget.state.nomadBookmarks;
    final nodes = widget.state.nomadNodes.values.toList()
      ..sort((a, b) => b.lastSeen.compareTo(a.lastSeen));
    return ListView(
      padding: const EdgeInsets.all(14),
      children: [
        if (bookmarks.isNotEmpty) ...[
          _sectionLabel(tc, 'BOOKMARKS'),
          for (final mark in bookmarks) _bookmarkRow(tc, mark),
          const SizedBox(height: 16),
        ],
        _sectionLabel(tc, 'NODES HEARD ON THE MESH'),
        if (nodes.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 18),
            child: Text(
              'No Nomad Network nodes heard yet. Nodes appear here as their '
              'announces arrive over the mesh.',
              style:
                  TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
            ),
          ),
        for (final node in nodes) _nodeRow(tc, node),
      ],
    );
  }

  Widget _sectionLabel(TCSectionColors tc, String label) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        label,
        style: TextStyle(
          fontSize: TCType.textCaption,
          color: tc.textSecondary,
          letterSpacing:
              TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
        ),
      ),
    );
  }

  Widget _bookmarkRow(TCSectionColors tc, NomadBookmark mark) {
    return InkWell(
      onTap: () => _go('${mark.nodeHash}:${mark.path}'),
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 8),
        decoration: BoxDecoration(
          border: Border(bottom: BorderSide(color: tc.borderSubtle)),
        ),
        child: Row(
          children: [
            Text('★ ',
                style: TextStyle(
                    fontSize: TCType.textBodySm, color: tc.borderAccent)),
            Expanded(
              child: Text(
                mark.label.isNotEmpty ? mark.label : mark.path,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                    fontSize: TCType.textBodySm, color: tc.textEmphasis),
              ),
            ),
            Text(
              '${_shortHash(mark.nodeHash)}:${mark.path}',
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                  fontSize: TCType.textCaption, color: tc.textTertiary),
            ),
          ],
        ),
      ),
    );
  }

  Widget _nodeRow(TCSectionColors tc, NomadNode node) {
    return InkWell(
      onTap: () => _go(node.nodeHash),
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 8),
        decoration: BoxDecoration(
          border: Border(bottom: BorderSide(color: tc.borderSubtle)),
        ),
        child: Row(
          children: [
            TcIcon(TcIcons.globe, size: 14, color: tc.textSecondary),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                node.displayName.isNotEmpty ? node.displayName : 'unnamed node',
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                    fontSize: TCType.textBodySm, color: tc.textEmphasis),
              ),
            ),
            Text(
              _shortHash(node.nodeHash),
              style: TextStyle(
                  fontSize: TCType.textCaption, color: tc.textTertiary),
            ),
            const SizedBox(width: 10),
            Text(
              formatRelative(node.lastSeen),
              style: TextStyle(
                  fontSize: TCType.textCaption, color: tc.textTertiary),
            ),
          ],
        ),
      ),
    );
  }

  Widget _pageView(TCSectionColors tc) {
    final page = _page;
    if (page == null) {
      return Center(
        child: Text(
          _loading ? 'LOCATING NODE…' : 'No page loaded.',
          style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
        ),
      );
    }
    return SelectionArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: MicronView(source: page.source, onLinkTap: _onMicronLink),
      ),
    );
  }

  void _onMicronLink(String url) {
    if (url.contains(':/file/') || url.startsWith('/file/')) {
      _fetchFile(url);
      return;
    }
    _go(url);
  }

  /// Fetches a /file/ link into the backend cache without leaving the page.
  /// When it lands, the authenticated download URL goes to the clipboard --
  /// the same interim answer main_window's _openLink gives for web links.
  Future<void> _fetchFile(String url) async {
    final fetchId = await widget.state
        .browseNomad(url, currentNode: _current?.nodeHash);
    if (!mounted) return;
    if (fetchId == null) {
      setState(() =>
          _error = widget.state.takeActionError() ?? 'Could not fetch file.');
      return;
    }
    setState(() {
      _fileFetchId = fetchId;
      _info = 'Fetching file…';
    });
  }

  void _advanceFileFetch() {
    final fetchId = _fileFetchId;
    final status = fetchId == null ? null : widget.state.nomadFetches[fetchId];
    if (status == null || !status.isTerminal) return;
    widget.state.takeNomadFetch(fetchId!);
    _fileFetchId = null;
    if (status.status == 'done') {
      final uri = widget.state.api.nomadFileUri(status.nodeHash, status.path);
      Clipboard.setData(ClipboardData(text: uri.toString()));
      setState(() => _info =
          'File cached — download URL copied to clipboard.');
    } else {
      setState(() {
        _info = null;
        _error = _friendlyReason(status.reason);
      });
    }
  }

  String _shortHash(String hash) =>
      hash.length > 12 ? '${hash.substring(0, 12)}…' : hash;
}
