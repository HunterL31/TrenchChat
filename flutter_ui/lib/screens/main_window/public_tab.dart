// PUBLIC tab -- chat over RRC (Reticulum Relay Chat), the protocol rrcd,
// rrc-gui and rrc-web speak. This is the one part of TrenchChat with a centre
// in it: a hub relays every line and sees who is in which room, which no
// TrenchChat channel does. docs/rrc.md records why that was accepted.
//
// Nothing here is stored. A hub keeps no history, a link loss ends the
// session outright, and reconnecting is a new session with nothing carried
// over -- so the tab says so rather than implying continuity it does not have.
import 'dart:async';

import 'package:flutter/material.dart';

import '../../api/models/rrc.dart';
import '../../app_state.dart';
import '../../format.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_text_field.dart';
import '../../widgets/tc_tooltip.dart';
import '../dialogs/confirm_dialog.dart';

/// Width of the hub/room column beside the transcript.
const double _sideWidth = 220;

String shortHubHash(String hex) {
  if (hex.length <= 10) return hex;
  return '${hex.substring(0, 5)}…${hex.substring(hex.length - 5)}';
}

/// What to call a hub in a list: its announced name and its hash, never the
/// name alone. Any node can announce any name.
String hubLabel(RRCHub hub) =>
    hub.name.isEmpty ? shortHubHash(hub.hash) : hub.name;

class PublicTab extends StatefulWidget {
  const PublicTab({super.key, required this.state});

  final AppState state;

  @override
  State<PublicTab> createState() => _PublicTabState();
}

class _PublicTabState extends State<PublicTab> {
  final TextEditingController _compose = TextEditingController();
  final TextEditingController _roomField = TextEditingController();
  String? _room;

  @override
  void initState() {
    super.initState();
    widget.state.addListener(_onStateChanged);
    unawaited(_load(widget.state.takeRrcPendingLink()));
  }

  /// A link is acted on only once the surface has been read: whether we are
  /// already on that hub decides whether connecting is asked for at all, and
  /// before the read every hub looks unconnected.
  Future<void> _load(RRCLink? link) async {
    await widget.state.refreshRrc();
    await widget.state.refreshRrcHosting();
    if (link != null && mounted) await _openLink(link);
  }

  /// Opens an rrc:// link: the hub is confirmed like any other, because the
  /// link came from a page somebody else wrote and connecting hands the hub
  /// this node's identity.
  Future<void> _openLink(RRCLink link) async {
    if (!mounted) return;
    final known = widget.state.rrcState.hubs
        .where((h) => h.hash == link.hubHash)
        .toList();
    final hub = known.isEmpty
        ? RRCHub(
            hash: link.hubHash,
            name: '',
            heardAt: 0,
            bookmarked: false,
            connected: false)
        : known.first;
    if (!hub.connected) {
      await _connect(hub);
      if (!mounted) return;
    }
    final room = link.room;
    if (room == null || !widget.state.rrcState.session.isActive) return;
    final ok = await widget.state.joinRrcRoom(room);
    if (ok && mounted) setState(() => _room = room);
  }

  void _onStateChanged() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    widget.state.removeListener(_onStateChanged);
    _compose.dispose();
    _roomField.dispose();
    super.dispose();
  }

  /// The room shown, falling back to the first joined one so the pane is
  /// never blank while rooms are held.
  String? get _activeRoom {
    final rooms = widget.state.rrcState.session.rooms.keys.toList();
    if (_room != null && rooms.contains(_room)) return _room;
    return rooms.isEmpty ? null : rooms.first;
  }

  Future<void> _connect(RRCHub hub) async {
    final ok = await showTcConfirmDialog(
      context,
      widget.state,
      title: 'Connect to ${hubLabel(hub)}?',
      message: 'Connecting identifies this node to the hub. It will see your '
          'identity hash, every room you join and every line you type, for as '
          'long as the session lasts.\n\n${hub.hash}',
      confirmLabel: 'CONNECT',
    );
    if (!ok || !mounted) return;
    await widget.state.connectRrcHub(hub.hash);
  }

  Future<void> _joinRoom() async {
    final room = _roomField.text.trim();
    if (room.isEmpty) return;
    final ok = await widget.state.joinRrcRoom(room);
    if (!mounted) return;
    _roomField.clear();
    if (ok) setState(() => _room = room.startsWith('#') ? room : '#$room');
  }

  Future<void> _send() async {
    final room = _activeRoom;
    final text = _compose.text.trim();
    if (room == null || text.isEmpty) return;
    _compose.clear();
    await widget.state.sendRrcMessage(room, text);
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      color: tc.bgApp,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SizedBox(width: _sideWidth, child: _sidePane(context)),
          Container(width: 1, color: tc.borderSubtle),
          Expanded(child: _roomPane(context)),
        ],
      ),
    );
  }

  // --- hubs and rooms ---

  Widget _sidePane(BuildContext context) {
    final tc = SectionTheme.of(context);
    final state = widget.state;
    final session = state.rrcState.session;
    return SingleChildScrollView(
      padding: const EdgeInsets.all(14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _SectionLabel('HUBS HEARD'),
          const SizedBox(height: 8),
          if (state.rrcState.hubs.isEmpty)
            Text(
              'None yet. Hubs are heard from their announces, never looked '
              'up: there is no directory to ask.',
              key: const Key('rrc-no-hubs'),
              style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
            ),
          for (final hub in state.rrcState.hubs)
            _HubRow(
              hub: hub,
              onConnect: () => _connect(hub),
              onDisconnect: state.disconnectRrc,
              onToggleBookmark: () =>
                  state.setRrcBookmark(hub.hash, !hub.bookmarked),
            ),
          const SizedBox(height: 18),
          _SectionLabel('SESSION'),
          const SizedBox(height: 8),
          _sessionSummary(context, session),
          if (session.isActive) ...[
            const SizedBox(height: 18),
            _SectionLabel('ROOMS'),
            const SizedBox(height: 8),
            for (final room in session.rooms.keys)
              _RoomRow(
                room: room,
                selected: room == _activeRoom,
                onTap: () {
                  setState(() => _room = room);
                  if (!state.rrcLinesByRoom.containsKey(room)) {
                    state.loadRrcLines(room);
                  }
                },
                onPart: () => state.partRrcRoom(room),
              ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: TcTextField(
                    key: const Key('rrc-room-field'),
                    label: 'ROOM',
                    controller: _roomField,
                    hintText: '#room',
                    onSubmitted: (_) => _joinRoom(),
                  ),
                ),
                const SizedBox(width: 6),
                TcGhostButton(
                  key: const Key('rrc-join-room'),
                  icon: TcIcons.plus,
                  label: 'JOIN',
                  onPressed: _joinRoom,
                ),
              ],
            ),
          ],
          const SizedBox(height: 18),
          _SectionLabel('THIS NODE'),
          const SizedBox(height: 8),
          _NicknameField(state: state),
          const SizedBox(height: 8),
          _HostingRow(state: state),
        ],
      ),
    );
  }

  Widget _sessionSummary(BuildContext context, RRCSession session) {
    final tc = SectionTheme.of(context);
    final reason = widget.state.rrcSessionReason;
    if (session.hub == null) {
      return Text(
        reason.isEmpty ? 'Not connected.' : 'Not connected: $reason',
        key: const Key('rrc-session-idle'),
        style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
      );
    }
    final label = session.name.isEmpty
        ? shortHubHash(session.hub!)
        : '${session.name} (${shortHubHash(session.hub!)})';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          key: const Key('rrc-session-hub'),
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textPrimary),
        ),
        Text(
          session.state.toUpperCase(),
          key: const Key('rrc-session-state'),
          style: TextStyle(
            fontSize: TCType.textCaption,
            color: session.isActive ? tc.statusOnline : tc.statusWarn,
          ),
        ),
      ],
    );
  }

  // --- transcript ---

  Widget _roomPane(BuildContext context) {
    final tc = SectionTheme.of(context);
    final room = _activeRoom;
    if (room == null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(
            widget.state.rrcState.session.isActive
                ? 'Join a room to start.'
                : 'Connect to a hub to join a room.',
            key: const Key('rrc-empty'),
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
          ),
        ),
      );
    }
    final lines = widget.state.rrcLinesByRoom[room] ?? const <RRCLine>[];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(18, 14, 18, 6),
          child: Row(
            children: [
              Text(
                room,
                key: const Key('rrc-room-title'),
                style: TextStyle(
                  fontSize: TCType.textBodyMd,
                  color: tc.textPrimary,
                ),
              ),
              const SizedBox(width: 10),
              Text(
                '${widget.state.rrcState.rosters[room]?.length ?? 0} present',
                style: TextStyle(
                    fontSize: TCType.textCaption, color: tc.textTertiary),
              ),
            ],
          ),
        ),
        Expanded(
          child: ListView.builder(
            padding: const EdgeInsets.symmetric(horizontal: 18),
            itemCount: lines.length,
            itemBuilder: (context, i) => _LineRow(line: lines[i]),
          ),
        ),
        Padding(
          padding: const EdgeInsets.all(14),
          child: Row(
            children: [
              Expanded(
                child: TcTextField(
                  key: const Key('rrc-compose'),
                  label: 'MESSAGE',
                  controller: _compose,
                  hintText: 'Message $room',
                  onSubmitted: (_) => _send(),
                ),
              ),
              const SizedBox(width: 8),
              TcGhostButton(
                key: const Key('rrc-send'),
                icon: TcIcons.send,
                label: 'SEND',
                onPressed: _send,
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Text(
      text,
      style: TextStyle(
        fontSize: TCType.textCaption,
        color: tc.textSecondary,
        letterSpacing:
            TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
      ),
    );
  }
}

class _HubRow extends StatelessWidget {
  const _HubRow({
    required this.hub,
    required this.onConnect,
    required this.onDisconnect,
    required this.onToggleBookmark,
  });

  final RRCHub hub;
  final VoidCallback onConnect;
  final VoidCallback onDisconnect;
  final VoidCallback onToggleBookmark;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        children: [
          Expanded(
            child: TcTooltip(
              message: hub.hash,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    hubLabel(hub),
                    style: TextStyle(
                      fontSize: TCType.textBodySm,
                      color: hub.connected ? tc.accentPrimary : tc.textPrimary,
                    ),
                  ),
                  Text(
                    shortHubHash(hub.hash),
                    style: TextStyle(
                        fontSize: TCType.textCaption, color: tc.textTertiary),
                  ),
                ],
              ),
            ),
          ),
          TcGhostButton(
            key: Key('rrc-bookmark-${hub.hash}'),
            label: hub.bookmarked ? 'SAVED' : 'SAVE',
            onPressed: onToggleBookmark,
          ),
          const SizedBox(width: 4),
          TcGhostButton(
            key: Key('rrc-connect-${hub.hash}'),
            label: hub.connected ? 'LEAVE' : 'OPEN',
            onPressed: hub.connected ? onDisconnect : onConnect,
          ),
        ],
      ),
    );
  }
}

class _RoomRow extends StatelessWidget {
  const _RoomRow({
    required this.room,
    required this.selected,
    required this.onTap,
    required this.onPart,
  });

  final String room;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback onPart;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          Expanded(
            child: GestureDetector(
              onTap: onTap,
              child: Text(
                room,
                key: Key('rrc-room-$room'),
                style: TextStyle(
                  fontSize: TCType.textBodySm,
                  color: selected ? tc.accentPrimary : tc.textPrimary,
                ),
              ),
            ),
          ),
          TcGhostButton(
            key: Key('rrc-part-$room'),
            icon: TcIcons.close,
            label: 'PART',
            onPressed: onPart,
          ),
        ],
      ),
    );
  }
}

class _LineRow extends StatelessWidget {
  const _LineRow({required this.line});

  final RRCLine line;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final color = line.isNotice ? tc.textTertiary : tc.textPrimary;
    final body = line.isAction
        ? '* ${line.label} ${line.text}'
        : '<${line.label}> ${line.text}';
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            formatTsShort(line.at),
            style:
                TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: TcTooltip(
              // A nick is advisory and a hub may let two clients claim the
              // same one; the identity hash is the only part that is proof.
              message: line.source,
              child: Text(
                body,
                style: TextStyle(fontSize: TCType.textBodySm, color: color),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _NicknameField extends StatefulWidget {
  const _NicknameField({required this.state});

  final AppState state;

  @override
  State<_NicknameField> createState() => _NicknameFieldState();
}

class _NicknameFieldState extends State<_NicknameField> {
  late final TextEditingController _controller =
      TextEditingController(text: widget.state.rrcState.nickname);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return TcTextField(
      key: const Key('rrc-nickname'),
      label: 'NICKNAME',
      controller: _controller,
      hintText: 'advisory; a hub may refuse it',
      onSubmitted: (value) => widget.state.setRrcNickname(value.trim()),
    );
  }
}

class _HostingRow extends StatefulWidget {
  const _HostingRow({required this.state});

  final AppState state;

  @override
  State<_HostingRow> createState() => _HostingRowState();
}

class _HostingRowState extends State<_HostingRow> {
  /// Owned here rather than made per dialog: a dialog's dismiss animation
  /// rebuilds its field after the future completes, so a controller disposed
  /// alongside the dialog is used after it is gone.
  final TextEditingController _name = TextEditingController();

  AppState get state => widget.state;

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final hosting = state.rrcHosting;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                hosting.enabled
                    ? 'Hosting a hub: ${hosting.clients} client(s)'
                    : 'Not hosting a hub',
                key: const Key('rrc-hosting-state'),
                style: TextStyle(
                    fontSize: TCType.textBodySm, color: tc.textPrimary),
              ),
            ),
            TcGhostButton(
              key: const Key('rrc-hosting-toggle'),
              label: hosting.enabled ? 'STOP' : 'HOST',
              onPressed: () => hosting.enabled
                  ? state.setRrcHosting(enabled: false)
                  : _askToHost(context),
            ),
          ],
        ),
        if (hosting.enabled && hosting.hubHash.isNotEmpty)
          Text(
            hosting.hubHash,
            key: const Key('rrc-hosting-hash'),
            style:
                TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
          ),
      ],
    );
  }

  Future<void> _askToHost(BuildContext context) async {
    _name.text = state.rrcHosting.name.isEmpty ? 'trenchchat' : state.rrcHosting.name;
    final name = await showTcDialog<String>(
      context: context,
      builder: (context) => SectionTheme(
        spec: state.themeSpec,
        section: TCSection.dialogs,
        child: Builder(
          builder: (context) => TcDialogShell(
            title: 'Host a hub',
            actions: [
              TcGhostButton(
                  label: 'CANCEL', onPressed: () => Navigator.pop(context)),
              TcPrimaryButton(
                key: const Key('rrc-hosting-confirm'),
                label: 'START',
                onPressed: () => Navigator.pop(context, _name.text.trim()),
              ),
            ],
            children: [
              Text(
                'Your node announces itself as an RRC hub and relays other '
                "people's rooms. It holds nothing: a hub anyone can run is a "
                'hub anyone can replace.',
                style: TextStyle(
                  fontSize: TCType.textBodySm,
                  height: TCType.leadingBody,
                  color: SectionTheme.of(context).textSecondary,
                ),
              ),
              const SizedBox(height: 12),
              TcTextField(
                key: const Key('rrc-hosting-name'),
                label: 'HUB NAME',
                controller: _name,
              ),
            ],
          ),
        ),
      ),
    );
    if (name == null || name.isEmpty) return;
    await state.setRrcHosting(enabled: true, hubName: name);
  }
}
