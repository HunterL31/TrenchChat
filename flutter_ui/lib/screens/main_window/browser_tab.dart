// NET tab -- a Nomad Network page browser. Discovered nodes and bookmarks
// when idle; fetched micron pages once a node is opened. Fetches run on the
// backend (POST /nomad/browse) and complete via nomad_fetch WS events; the
// page content itself is pulled from the cache endpoint afterwards, so a
// previously seen page renders instantly and refreshes in place.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../api/models/nomad.dart';
import '../../app_state.dart';
import '../../format.dart';
import '../../micron/micron_parser.dart';
import '../../micron/micron_view.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';
import '../dialogs/confirm_dialog.dart';
import '../dialogs/nomad_hosting_dialog.dart';
import '../dialogs/rename_bookmark_dialog.dart';

class BrowserTab extends StatefulWidget {
  const BrowserTab({super.key, required this.state});

  final AppState state;

  @override
  State<BrowserTab> createState() => _BrowserTabState();
}

class _BrowserTabState extends State<BrowserTab> {
  final TextEditingController _address = TextEditingController();

  String? _activeFetchId;
  String? _fileFetchId;
  NomadPage? _page;

  /// Where [_page] came from. The page pane only shows content that belongs
  /// to the location being browsed -- a fetch in flight must never leave the
  /// previous page on screen looking like the answer.
  ({String nodeHash, String path})? _pageLocation;

  /// [_page]'s own `#!bg=` colour, parsed once when the page lands so the
  /// whole pane can carry it rather than only the text column.
  Color? _pageBackground;

  /// The anchor a link's `anchor=` variable asked the next page to open at,
  /// held until that page renders.
  String? _pendingAnchor;
  String? _error;
  String? _info;
  double _progress = 0;
  bool _loading = false;

  /// Fetch results arrive as WebSocket events, which are gone for good if the
  /// socket is down when one fires. This asks the backend directly while
  /// anything is in flight, so a dropped socket costs latency, not the page.
  Timer? _pollTimer;
  static const Duration _pollInterval = Duration(seconds: 2);

  /// Our own hosting, so the node list can offer the pages we serve. They
  /// are read straight off disk -- a node cannot dial itself.
  NomadHosting? _hosting;

  ({String nodeHash, String path})? get _current =>
      widget.state.nomadLocation;

  @override
  void initState() {
    super.initState();
    widget.state.addListener(_onStateChanged);
    if (widget.state.nomadNodes.isEmpty) {
      widget.state.loadNomadNodes();
    }
    widget.state.loadNomadBookmarks();
    _loadHosting();
    final pending = widget.state.takePendingNomadUrl();
    if (pending != null) {
      Future.microtask(() => _go(pending));
      return;
    }
    // Remounted on the page we left: the trail survived in AppState, the
    // rendered page did not, so read it back rather than showing an empty
    // pane over a location we still claim to be on.
    final resumed = _current;
    if (resumed != null) {
      _address.text = '${resumed.nodeHash}:${resumed.path}';
      Future.microtask(() => _resume(resumed));
    }
  }

  /// Restores the page for a location we are already on. Falls back to
  /// fetching when it has been pruned from the cache since.
  Future<void> _resume(({String nodeHash, String path}) location) async {
    final shown = await _showCached(location.nodeHash, location.path);
    if (!shown && mounted) {
      _go('${location.nodeHash}:${location.path}');
    }
  }

  Future<void> _loadHosting() async {
    try {
      final hosting = await widget.state.api.getNomadHosting();
      if (mounted) setState(() => _hosting = hosting);
    } catch (_) {
      // Hosting is only an extra row here; the node list stands without it.
    }
  }

  @override
  void dispose() {
    widget.state.removeListener(_onStateChanged);
    _pollTimer?.cancel();
    _address.dispose();
    super.dispose();
  }

  /// Runs only while a fetch is outstanding.
  void _syncPolling() {
    final waiting = _activeFetchId != null || _fileFetchId != null;
    if (waiting && _pollTimer == null) {
      _pollTimer = Timer.periodic(_pollInterval, (_) => _pollFetches());
    } else if (!waiting) {
      _pollTimer?.cancel();
      _pollTimer = null;
    }
  }

  void _pollFetches() {
    if (!mounted) return;
    for (final fetchId in {_activeFetchId, _fileFetchId}) {
      if (fetchId != null) widget.state.pollNomadFetch(fetchId);
    }
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
      _syncPolling();
      setState(() {});
      return;
    }
    if (!status.isTerminal) {
      _syncPolling();
      setState(() => _progress = status.progress);
      return;
    }
    widget.state.takeNomadFetch(fetchId!);
    _activeFetchId = null;
    _syncPolling();
    if (status.status == 'done') {
      setState(() {});
      _showCached(status.nodeHash, status.path, doneLoading: true);
    } else {
      // A fetch the backend has forgotten may still have landed in the cache
      // before this client lost track of it; show that rather than an error.
      _recoverOrFail(status);
    }
  }

  Future<void> _recoverOrFail(NomadFetchStatus status) async {
    if (status.reason == 'forgotten' && status.nodeHash.isNotEmpty) {
      final shown = await _showCached(status.nodeHash, status.path);
      if (!mounted) return;
      if (shown) {
        setState(() {
          _loading = false;
          _error = null;
        });
        return;
      }
    }
    if (!mounted) return;
    setState(() {
      _loading = false;
      _error = _friendlyReason(status.reason);
    });
  }

  /// Shows the cached copy of a location, if there is one. Returns whether
  /// there was.
  Future<bool> _showCached(String nodeHash, String path,
      {bool doneLoading = false}) async {
    final page = await widget.state.fetchCachedNomadPage(nodeHash, path);
    if (!mounted) return page != null;
    setState(() {
      if (page != null) {
        _page = page;
        _pageLocation = (nodeHash: nodeHash, path: path);
        _pageBackground = micronPageBackground(page.source);
      }
      if (doneLoading) {
        _loading = false;
        _error = page == null ? 'Fetched page could not be read back.' : null;
      }
    });
    return page != null;
  }

  Future<void> _go(String url,
      {Map<String, String>? data, bool refresh = false}) async {
    if (url.trim().isEmpty) return;
    // Micron's own convention: a link carrying anchor= names where in the
    // page it lands should open. The node never sees it as a scroll
    // instruction -- it is an ordinary request variable -- so it is read
    // here as well as sent.
    _pendingAnchor = data?['var_anchor'];
    final target = await widget.state.browseNomad(url.trim(),
        currentNode: _current?.nodeHash, data: data, refresh: refresh);
    if (!mounted) return;
    if (target == null) {
      setState(() =>
          _error = widget.state.takeActionError() ?? 'Could not open that URL.');
      return;
    }
    final nodeHash = target.nodeHash;
    final path = target.path;
    // A repeat visit while the same fetch is still in flight reuses its id;
    // only push history when the location actually changes.
    widget.state.pushNomadLocation(nodeHash, path);
    if (widget.state.nomadIdentify[nodeHash] == null) {
      widget.state.loadNomadIdentify(nodeHash);
    }
    setState(() {
      _activeFetchId = target.fetchId;
      _loading = target.fetchId != null;
      _error = null;
      _info = null;
      _progress = 0;
      _address.text = '$nodeHash:$path';
    });
    // Stale-while-revalidate: show whatever we already have immediately.
    // For a page the node told us to cache there is nothing else coming.
    _showCached(nodeHash, path);
    if (target.fetchId == null) {
      _syncPolling();
      return;
    }
    // The fetch may have finished before _activeFetchId was assigned, in
    // which case its terminal event already fired and nothing else will;
    // claim it now rather than waiting for an unrelated notification.
    _onStateChanged();
  }

  Future<void> _navigateHistory(int delta) async {
    final entry = widget.state.stepNomadHistory(delta);
    if (entry == null) return;
    setState(() {
      _error = null;
      _activeFetchId = null;
      _loading = false;
      _address.text = '${entry.nodeHash}:${entry.path}';
    });
    // A page pruned from the cache since it was visited has to be fetched
    // again; _go will not re-push history for the location we just moved to.
    final cached = await _showCached(entry.nodeHash, entry.path);
    if (!cached && mounted) _go('${entry.nodeHash}:${entry.path}');
  }

  /// RELOAD always goes back to the node, even for a page still inside the
  /// lifetime it declared -- that is what the button is for.
  Future<void> _reload() async {
    final current = _current;
    if (current == null) return;
    await _go('${current.nodeHash}:${current.path}', refresh: true);
  }

  void _home() {
    widget.state.clearNomadHistory();
    setState(() {
      _page = null;
      _pageLocation = null;
      _pageBackground = null;
      _error = null;
      _info = null;
      _activeFetchId = null;
      _loading = false;
      _address.text = '';
    });
    widget.state.loadNomadNodes();
    widget.state.loadNomadBookmarks();
    _loadHosting();
  }

  /// Turns identifying to the node being browsed on or off.
  ///
  /// Identifying is irreversible in the only sense that matters: the node
  /// operator learns this identity visited and can keep that forever, so
  /// turning it on asks first and turning it off says what it does not undo.
  Future<void> _toggleIdentify() async {
    final current = _current;
    if (current == null) return;
    final status = widget.state.nomadIdentify[current.nodeHash] ??
        await widget.state.loadNomadIdentify(current.nodeHash);
    if (!mounted || status == null) return;
    final turningOn = !status.enabled;

    final confirmed = await showTcConfirmDialog(
      context,
      widget.state,
      title: turningOn ? 'Identify to this node?' : 'Stop identifying?',
      message: turningOn
          ? 'This node will see your identity hash '
              '${status.identityHash} on every page you open here, and can '
              'keep a record of it. Pages that need an account — a forum, '
              'anything with a login — use it as your account. Nothing '
              'about your Reticulum instance or your other nodes is '
              'revealed, and no other node is affected.'
          : 'This node stops seeing your identity from the next page you '
              'open — the connection carrying it is dropped. What it '
              'already recorded about this identity stays recorded.',
      confirmLabel: turningOn ? 'IDENTIFY' : 'STOP',
    );
    if (!confirmed || !mounted) return;

    final updated =
        await widget.state.setNomadIdentify(current.nodeHash, turningOn);
    if (!mounted) return;
    if (updated == null) {
      setState(() => _error =
          widget.state.takeActionError() ?? 'Could not change identifying.');
      return;
    }
    // The page the node served was rendered for whoever it thought we were,
    // and navigating clears the banner -- so reload first, then say so.
    if (turningOn) await _reload();
    if (!mounted) return;
    setState(() => _info = turningOn
        ? (updated.identified
            ? 'Identified to this node. This page is how it sees you now.'
            : 'Identifying from the next connection to this node.')
        : 'No longer identifying to this node.');
  }

  Future<void> _toggleBookmark() async {
    final current = _current;
    if (current == null) return;
    await widget.state.toggleNomadBookmark(
        current.nodeHash, current.path, _defaultBookmarkLabel(current));
  }

  /// What to call a bookmark when the user has not said.
  ///
  /// The node's name alone was the same string for every page on it, which
  /// made a shelf of bookmarks to one node unreadable. A page's own heading
  /// names the page; failing that the node and the file it came from do.
  String _defaultBookmarkLabel(({String nodeHash, String path}) location) {
    final page = _page;
    if (page != null &&
        _pageLocation?.nodeHash == location.nodeHash &&
        _pageLocation?.path == location.path) {
      final title = micronPageTitle(page.source);
      if (title.isNotEmpty) return title;
    }
    final node = widget.state.nomadNodes[location.nodeHash]?.displayName ?? '';
    final basename = location.path.split('/').last;
    if (node.isNotEmpty && basename.isNotEmpty) return '$node — $basename';
    return location.path;
  }

  Future<void> _renameBookmark(NomadBookmark mark) async {
    final label = await showRenameBookmarkDialog(
        context, widget.state, current: mark.label);
    if (label == null || !mounted) return;
    await widget.state
        .renameNomadBookmark(mark.nodeHash, mark.path, label.trim());
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
        'not_found' => 'Your node does not serve that path. Add the file '
            'under its pages directory and press RELOAD.',
        'send_failed' => 'The link to the node could not carry the request.',
        'forgotten' => 'Lost track of that fetch. Reload to try again.',
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
            // Determinate (an indeterminate bar animates forever), but with
            // a visible floor: the whole dial phase reports zero progress,
            // and a bar at exactly zero paints nothing at all.
            LinearProgressIndicator(
              value: _progress > 0.05 ? _progress : 0.05,
              minHeight: 3,
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
    final identified = current != null &&
        (widget.state.nomadIdentify[current.nodeHash]?.enabled ?? false);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: tc.borderDefault)),
      ),
      child: Row(
        children: [
          TcGhostButton(
              label: '<',
              onPressed: widget.state.nomadHistoryIndex > 0
                  ? () => _navigateHistory(-1)
                  : null),
          const SizedBox(width: 4),
          TcGhostButton(
              label: '>',
              onPressed: widget.state.nomadHistoryIndex <
                      widget.state.nomadHistory.length - 1
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
            icon: TcIcons.lock,
            label: identified ? 'ID ✓' : 'ID',
            onPressed: current == null ? null : _toggleIdentify,
          ),
          const SizedBox(width: 4),
          TcGhostButton(
            label: 'HOST',
            onPressed: () async {
              await showNomadHostingDialog(context, widget.state);
              await _loadHosting();
            },
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
    final hosting = _hosting;
    return ListView(
      padding: const EdgeInsets.all(14),
      children: [
        if (hosting != null &&
            hosting.enabled &&
            hosting.nodeHash.isNotEmpty) ...[
          _sectionLabel(tc, 'YOUR NODE'),
          _ownNodeRow(tc, hosting),
          const SizedBox(height: 16),
        ],
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

  Widget _ownNodeRow(TCSectionColors tc, NomadHosting hosting) {
    return InkWell(
      onTap: () => _go(hosting.nodeHash),
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 8),
        decoration: BoxDecoration(
          border: Border(bottom: BorderSide(color: tc.borderSubtle)),
        ),
        child: Row(
          children: [
            TcIcon(TcIcons.globe, size: 14, color: tc.borderAccent),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                hosting.nodeName.isNotEmpty ? hosting.nodeName : 'your node',
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                    fontSize: TCType.textBodySm, color: tc.textEmphasis),
              ),
            ),
            Text(
              _shortHash(hosting.nodeHash),
              style: TextStyle(
                  fontSize: TCType.textCaption, color: tc.textTertiary),
            ),
            const SizedBox(width: 10),
            Text(
              'served from disk',
              style: TextStyle(
                  fontSize: TCType.textCaption, color: tc.textTertiary),
            ),
          ],
        ),
      ),
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
            const SizedBox(width: 8),
            TcGhostButton(
                label: 'RENAME', onPressed: () => _renameBookmark(mark)),
          ],
        ),
      ),
    );
  }

  /// Past this silence a node row dims: several announce intervals have been
  /// missed, so an identically-named live node is distinguishable at a
  /// glance from a dead one still in the list.
  static const double _nodeStaleAfterSecs = 1800;

  Widget _nodeRow(TCSectionColors tc, NomadNode node) {
    final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
    final stale = now - node.lastSeen > _nodeStaleAfterSecs;
    return InkWell(
      onTap: () => _go(node.nodeHash),
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 8),
        decoration: BoxDecoration(
          border: Border(bottom: BorderSide(color: tc.borderSubtle)),
        ),
        child: Row(
          children: [
            TcIcon(TcIcons.globe, size: 14,
                color: stale ? tc.textDisabled : tc.textSecondary),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                node.displayName.isNotEmpty ? node.displayName : 'unnamed node',
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                    fontSize: TCType.textBodySm,
                    color: stale ? tc.textTertiary : tc.textEmphasis),
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
    final current = _current;
    final showing = page != null &&
        current != null &&
        _pageLocation?.nodeHash == current.nodeHash &&
        _pageLocation?.path == current.path;
    if (!showing) return _pendingPane(tc, current);
    return Container(
      color: _pageBackground,
      child: SelectionArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: MicronView(
            source: page.source,
            onLinkTap: _onMicronLink,
            onPartialLoad: _loadPartial,
            initialAnchor: _pendingAnchor,
          ),
        ),
      ),
    );
  }

  /// What the page pane shows while a location has no content of its own:
  /// which location is being fetched, or that nothing came back for it.
  Widget _pendingPane(
      TCSectionColors tc, ({String nodeHash, String path})? current) {
    if (current == null) {
      return Center(
        child: Text('No page loaded.',
            style:
                TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary)),
      );
    }
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            _loading ? 'FETCHING' : 'NOTHING CACHED FOR',
            style: TextStyle(
              fontSize: TCType.textCaption,
              color: tc.textSecondary,
              letterSpacing: TCType.letterSpacingFor(
                  TCType.textCaption, TCType.trackingWider),
            ),
          ),
          const SizedBox(height: 8),
          Text(
            '${_shortHash(current.nodeHash)}:${current.path}',
            style:
                TextStyle(fontSize: TCType.textBodySm, color: tc.textEmphasis),
          ),
          const SizedBox(height: 10),
          if (_loading)
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 460),
              child: Text(
                'Pages come back over the mesh — a distant node can take a '
                'while, and one that does not serve this path never answers.',
                textAlign: TextAlign.center,
                style: TextStyle(
                    fontSize: TCType.textCaption, color: tc.textTertiary),
              ),
            )
          // The error banner carries its own RETRY; a second one here would
          // be two buttons for one action.
          else if (_error == null)
            TcGhostButton(label: 'RETRY', onPressed: _reload),
        ],
      ),
    );
  }

  void _onMicronLink(String url, Map<String, String> data) {
    final target = url.trim();
    final unsupported = _unsupportedScheme(target);
    if (unsupported != null) {
      setState(() => _info = unsupported);
      return;
    }
    if (target.contains(':/file/') || target.startsWith('/file/')) {
      _fetchFile(target);
      return;
    }
    _go(target, data: data);
  }

  /// Micron links can name destinations other than a node's pages. Saying
  /// which is better than the URL parser's complaint about hex digits.
  String? _unsupportedScheme(String url) {
    final lower = url.toLowerCase();
    if (lower.startsWith('lxmf@')) {
      return 'That link opens an LXMF conversation. TrenchChat messages '
          'mutual friends — add the peer from the FRIENDS tab first.';
    }
    if (lower.startsWith('rrc://')) {
      return 'That is a link to an RRC voice hub, which this browser does '
          'not open.';
    }
    return null;
  }

  /// Fetches one partial's page. Resolved against the node being browsed,
  /// so a partial can name a path the way a link does.
  Future<String?> _loadPartial(String url, Map<String, String> data) =>
      widget.state.loadNomadPartial(url,
          currentNode: _current?.nodeHash, data: data);

  /// Fetches a /file/ link into the backend cache without leaving the page.
  /// When it lands, the authenticated download URL goes to the clipboard --
  /// the same interim answer main_window's _openLink gives for web links.
  Future<void> _fetchFile(String url) async {
    final target = await widget.state
        .browseNomad(url, currentNode: _current?.nodeHash);
    if (!mounted) return;
    // Files are never answered from cache, so a fetch id is always expected.
    if (target?.fetchId == null) {
      setState(() =>
          _error = widget.state.takeActionError() ?? 'Could not fetch file.');
      return;
    }
    setState(() {
      _fileFetchId = target!.fetchId;
      _info = 'Fetching file…';
    });
    _syncPolling();
  }

  void _advanceFileFetch() {
    final fetchId = _fileFetchId;
    final status = fetchId == null ? null : widget.state.nomadFetches[fetchId];
    if (status == null || !status.isTerminal) return;
    widget.state.takeNomadFetch(fetchId!);
    _fileFetchId = null;
    _syncPolling();
    if (status.status == 'done') {
      final uri = widget.state.api.nomadFileUri(status.nodeHash, status.path);
      _copyFileUrl(uri.toString());
    } else {
      setState(() {
        _info = null;
        _error = _friendlyReason(status.reason);
      });
    }
  }

  /// Clipboard access is a permission the browser can refuse, so the banner
  /// reports what actually happened rather than assuming.
  Future<void> _copyFileUrl(String url) async {
    var copied = true;
    try {
      await Clipboard.setData(ClipboardData(text: url));
    } catch (_) {
      copied = false;
    }
    if (!mounted) return;
    setState(() => _info = copied
        ? 'File cached — download URL copied to clipboard.'
        : 'File cached, but this browser refused clipboard access, so the '
            'download URL could not be copied.');
  }

  String _shortHash(String hash) =>
      hash.length > 12 ? '${hash.substring(0, 12)}…' : hash;
}
