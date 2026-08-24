"""
Main application window.

Layout:
  ┌─────────────────────────────────────────────┐
  │  [+] New Channel   [Settings]               │  ← toolbar
  ├──────────────┬──────────────────────────────┤
  │              │  [Chat] [Network Map] [⚙ Interfaces] │  ← tabs
  │  Channel     ├──────────────────────────────┤
  │  list        │   Message view / map / iface │
  │              │                              │
  │  Online      ├──────────────────────────────┤
  │  users       │   Compose (chat tab only)    │
  └──────────────┴──────────────────────────────┘
"""

import RNS

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QSplitter, QToolBar,
    QLabel, QDialog, QFormLayout, QLineEdit, QComboBox,
    QDialogButtonBox, QMessageBox, QStackedWidget, QMenu,
    QPushButton, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSizePolicy, QTabWidget, QCheckBox,
)
from PyQt6.QtCore import Qt, pyqtSlot, QPoint, pyqtSignal, QTimer, QSettings
from PyQt6.QtGui import QAction, QColor, QFont

from trenchchat.config import Config
from trenchchat.core import actions
from trenchchat.core.connectivity import LinkWatcher
from trenchchat.core.sync import SYNC_RETRY_SECS
from trenchchat.core.identity import Identity
from trenchchat.core.image import prepare_image, MAX_IMAGE_BYTES
from trenchchat.core.naming import NameInUseError
from trenchchat.core.permissions import (
    CREATE_CHANNEL, INVITE, MANAGE_CHANNEL, SEND_MESSAGE, PRESETS, PRESET_PRIVATE,
    is_discoverable, is_open_join, permissions_from_json,
)
from trenchchat.core.presence import PresenceBeacon, PresenceManager, resolve_display_name
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.core.reaction import ReactionManager
from trenchchat.core.sync import SyncManager
from trenchchat.core.sync_status import SyncState
from trenchchat.core.user_directory import UserDirectory
from trenchchat.network.router import Router
from trenchchat.network.announce import PeerAnnounceHandler
from trenchchat.gui.channel_view import ChannelView
from trenchchat.gui.compose import ComposeWidget
from trenchchat.gui.emoji_picker import EmojiPicker
from trenchchat.gui.network_map import NetworkMapWidget, gather_network_data
from trenchchat.gui.settings import SettingsDialog
from trenchchat.gui.invite_dialogs import ChannelPermissionsDialog, InviteDialog, MembersDialog
from trenchchat.gui.interfaces_widget import InterfacesWidget

_STARTUP_SYNC_DELAY_MS = 3_000
# Checked more often than SYNC_RETRY_SECS, so a request that ages out is
# re-asked promptly rather than up to a full interval late.
_SYNC_TICK_INTERVAL_MS = int(SYNC_RETRY_SECS * 1000 / 3)
_PRESENCE_PRUNE_INTERVAL_MS = 30_000
_ANNOUNCE_DEBOUNCE_MS = 2_000

# Server header rows carry their hash here rather than under UserRole, which is
# reserved for channel hashes so header rows can never be selected as channels.
SERVER_HASH_ROLE = Qt.ItemDataRole.UserRole + 1


class NewChannelDialog(QDialog):
    def __init__(self, parent=None, in_server: bool = False):
        super().__init__(parent)
        self.setWindowTitle("New Channel in Server" if in_server else "New Channel")
        layout = QFormLayout(self)

        self._name = QLineEdit()
        self._name.setPlaceholderText("general")
        layout.addRow("Name:", self._name)

        self._desc = QLineEdit()
        layout.addRow("Description:", self._desc)

        # A channel in a server inherits the server's permissions, so offering
        # a preset here would imply an override that does not exist.
        self._preset = None
        if not in_server:
            self._preset = QComboBox()
            self._preset.addItems(list(PRESETS.keys()))
            layout.addRow("Preset:", self._preset)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    @property
    def channel_name(self) -> str:
        return self._name.text().strip()

    @property
    def description(self) -> str:
        return self._desc.text().strip()

    @property
    def permissions(self) -> dict:
        if self._preset is None:
            return dict(PRESET_PRIVATE)
        return dict(PRESETS.get(self._preset.currentText(), PRESET_PRIVATE))


class NewServerDialog(QDialog):
    """Servers are always invite-only, so there is no access choice to make."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Server")
        layout = QFormLayout(self)

        self._name = QLineEdit()
        self._name.setPlaceholderText("my-server")
        layout.addRow("Name:", self._name)

        self._desc = QLineEdit()
        layout.addRow("Description:", self._desc)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    @property
    def server_name(self) -> str:
        return self._name.text().strip()

    @property
    def description(self) -> str:
        return self._desc.text().strip()


class JoinChannelDialog(QDialog):
    """Lists discovered public channels the user hasn't subscribed to yet."""

    def __init__(self, storage: Storage, channel_mgr, router, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Join Channel")
        self.setMinimumSize(500, 320)
        self._storage = storage
        self._channel_mgr = channel_mgr
        self._router = router
        self._selected_hash: str | None = None

        layout = QVBoxLayout(self)

        hint = QLabel("Channels announced on the network appear here. "
                      "Click Refresh to request fresh announcements.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px; padding: 4px;")
        layout.addWidget(hint)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Name", "Description", "Creator"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        self._refresh_btn = QPushButton("↻ Refresh")
        self._refresh_btn.clicked.connect(self._on_refresh)
        btn_row.addWidget(self._refresh_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Join")
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._populate()
        # Trigger a re-announce on open so peers hear us and may re-announce back.
        self._channel_mgr.announce_all_owned()
        self._router.announce()

    def _populate(self):
        self._table.setRowCount(0)
        self._hashes: list[str] = []
        for row in self._storage.get_all_channels():
            if self._storage.is_subscribed(row["hash"]):
                continue
            perms = permissions_from_json(row["permissions"])
            if not is_discoverable(perms):
                continue
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, 0, QTableWidgetItem(row["name"]))
            self._table.setItem(r, 1, QTableWidgetItem(row["description"] or ""))
            self._table.setItem(r, 2, QTableWidgetItem(row["creator_hash"][:12] + "…"))
            self._hashes.append(row["hash"])

    def _on_refresh(self):
        """Re-announce our own channels and repopulate the table after a short delay."""
        self._channel_mgr.announce_all_owned()
        self._router.announce()
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Refreshing…")
        QTimer.singleShot(3000, self._after_refresh)

    def _after_refresh(self):
        self._populate()
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("↻ Refresh")

    def _on_selection_changed(self):
        rows = self._table.selectedItems()
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(rows))
        if rows:
            self._selected_hash = self._hashes[self._table.currentRow()]

    def _on_double_click(self, index):
        self._selected_hash = self._hashes[index.row()]
        self.accept()

    @property
    def selected_channel_hash(self) -> str | None:
        return self._selected_hash


class MainWindow(QMainWindow):
    # Signals used to safely marshal background-thread events onto the Qt main thread.
    _invite_received      = pyqtSignal(str, str, bytes, float, str)
    _message_received     = pyqtSignal(str, str)   # channel_hash_hex, message_id
    _channel_discovered   = pyqtSignal(str, str)   # channel_hash_hex, channel_name
    _channel_joined       = pyqtSignal(str, str)   # channel_hash_hex, channel_name
    _member_list_updated  = pyqtSignal(str)         # channel_hash_hex
    _presence_changed     = pyqtSignal(str, bool)   # peer_hex, is_online
    _peer_announced       = pyqtSignal()            # any announce → maybe refresh map
    _reannounce_requested = pyqtSignal(object)      # iface or None → start debounce timer
    _avatar_updated       = pyqtSignal(str)         # identity_hash_hex
    _reaction_updated     = pyqtSignal(str, str)    # channel_hash_hex, message_id
    _emoji_received       = pyqtSignal(str)         # emoji_hash — new emoji image arrived
    _sync_status_changed  = pyqtSignal(str)         # channel_hash_hex

    def __init__(self, config: Config, identity: Identity, storage: Storage,
                 rns: "RNS.Reticulum", router: Router, channel_mgr: ChannelManager,
                 messaging: Messaging, subscription_mgr: SubscriptionManager,
                 invite_mgr: InviteManager, presence_mgr: PresenceManager,
                 user_directory: UserDirectory, avatar_mgr=None, reaction_mgr=None,
                 server_mgr=None, presence_beacon: PresenceBeacon | None = None,
                 voice_mgr=None, friends_mgr=None):
        super().__init__()
        self._config = config
        self._identity = identity
        self._storage = storage
        self._rns = rns
        self._router = router
        self._channel_mgr = channel_mgr
        self._server_mgr = server_mgr
        self._messaging = messaging
        self._subscription_mgr = subscription_mgr
        self._invite_mgr = invite_mgr
        self._presence_mgr = presence_mgr
        self._presence_beacon = presence_beacon
        self._user_directory = user_directory
        self._avatar_mgr = avatar_mgr
        self._reaction_mgr: ReactionManager | None = reaction_mgr
        # Stored only for now; the voice UI lands with the frontend rework.
        # That UI must gate its join control on has_permission(channel, self,
        # VOICE_CHAT) and marshal VoiceManager callbacks through Qt signals.
        self._voice_mgr = voice_mgr
        self._friends_mgr = friends_mgr

        # Pending invites: list of (channel_hash_hex, channel_name, token, expiry, admin_hash_hex)
        self._pending_invites: list[tuple] = []

        self._channel_views: dict[str, ChannelView] = {}
        # channel_hash_hex -> SyncState value, for channels currently syncing or
        # incomplete; entries are removed once a channel settles.
        self._channel_sync_state: dict[str, str] = {}
        self._current_channel: str | None = None
        self._settings = QSettings("TrenchChat", "TrenchChat")
        # Server hashes whose channels are hidden in the sidebar. QSettings
        # round-trips a single-element list as a bare string, hence the coerce.
        stored = self._settings.value("collapsed_servers", []) or []
        if isinstance(stored, str):
            stored = [stored]
        self._collapsed_servers: set[str] = set(stored)

        self.setWindowTitle("TrenchChat")
        self.setMinimumSize(800, 600)
        self._apply_dark_theme()
        self._network_map_timer: QTimer | None = None   # periodic refresh; created after _build_ui
        self._map_debounce_timer: QTimer | None = None  # debounce for announce-triggered refreshes
        self._build_ui()

        # Connect thread-safe signals to main-thread handlers
        self._invite_received.connect(self._on_invite_received_main_thread)
        self._message_received.connect(self._on_new_message_main_thread)
        self._channel_discovered.connect(self._on_channel_discovered_main_thread)
        self._channel_joined.connect(self._on_channel_joined_main_thread)
        self._member_list_updated.connect(self._on_member_list_updated_main_thread)
        self._presence_changed.connect(self._on_presence_changed_main_thread)
        self._peer_announced.connect(self._schedule_map_refresh)

        messaging.add_message_callback(self._on_new_message)
        invite_mgr.add_invite_callback(self._on_incoming_invite)
        invite_mgr.add_channel_joined_callback(self._on_channel_joined)
        invite_mgr.add_member_list_callback(self._on_member_list_updated)
        channel_mgr.add_channel_discovered_callback(self._on_channel_discovered)
        presence_mgr.add_presence_callback(self._on_presence_changed)

        # Restore invites received before a previous restart, but not yet
        # accepted or declined.
        for inv in invite_mgr.list_pending_invites():
            self._pending_invites.append((
                inv["channel_hash_hex"], inv["channel_name"], inv["token"],
                inv["expiry"], inv["admin_hash_hex"],
            ))
        self._update_invite_bar()

        self._avatar_updated.connect(self._on_avatar_updated_main_thread)
        if avatar_mgr is not None:
            avatar_mgr.add_avatar_callback(self._avatar_updated.emit)

        self._reaction_updated.connect(self._on_reaction_updated_main_thread)
        self._emoji_received.connect(self._on_emoji_received_main_thread)
        if reaction_mgr is not None:
            reaction_mgr.add_reaction_callback(self._reaction_updated.emit)
            reaction_mgr.add_emoji_callback(self._emoji_received.emit)

        self._sync_mgr = SyncManager(
            identity, storage, router, messaging, subscription_mgr, invite_mgr,
            reaction_mgr=reaction_mgr,
        )
        self._sync_status_changed.connect(self._on_sync_status_changed_main_thread)
        self._sync_mgr.status.add_status_callback(self._sync_status_changed.emit)

        def _on_peer_appeared(peer_hex: str, iface) -> None:
            self._sync_mgr.on_peer_appeared(peer_hex)
            self._presence_mgr.record_seen(peer_hex)
            self._seed_user_directory(peer_hex)
            if self._avatar_mgr is not None:
                self._avatar_mgr.flush_avatar(peer_hex)
            if self._reaction_mgr is not None:
                self._reaction_mgr.flush_pending_emoji(peer_hex)
            self._subscription_mgr.flush_pending(peer_hex)
            self._invite_mgr.flush_pending(peer_hex)
            if self._friends_mgr is not None:
                self._friends_mgr.flush_pending(peer_hex)
            self._peer_announced.emit()
            self._reannounce_requested.emit(iface)

        RNS.Transport.register_announce_handler(
            PeerAnnounceHandler(_on_peer_appeared)
        )

        # Also mark a peer as seen when any of their channel announces arrive.
        # trenchchat.channel announces fire once per owned channel per announce
        # cycle, so they are a reliable additional presence signal.
        # We also seed the user directory from channel announces: any peer that
        # announces a trenchchat.channel destination is definitively a TrenchChat
        # user, so we can add them without waiting for a trenchchat.user announce.
        def _on_channel_announce(destination_hash: bytes,
                                 announced_identity: "RNS.Identity",
                                 metadata: dict,
                                 iface) -> None:
            if announced_identity is not None:
                peer_hex = announced_identity.hash.hex()
                self._presence_mgr.record_seen(peer_hex)
                display_name = resolve_display_name(
                    peer_hex, self._identity.hash_hex, self._storage, self._config
                )
                self._user_directory.record_user(peer_hex, display_name)
            self._peer_announced.emit()
            self._reannounce_requested.emit(iface)

        from trenchchat.network.announce import ChannelAnnounceHandler
        RNS.Transport.register_announce_handler(
            ChannelAnnounceHandler(_on_channel_announce)
        )

        # Also update presence from any inbound LXMF message.  This covers the
        # case where a peer connects via a backchannel link and sends a message
        # without having announced their delivery destination first (which is the
        # normal LXMF direct-delivery flow), and it is where a peer's
        # going-offline notice takes effect.
        def _on_inbound_message(message: "LXMF.LXMessage") -> None:
            sender_hex = self._presence_mgr.record_inbound(message)
            if sender_hex:
                self._seed_user_directory(sender_hex)

        router.add_delivery_callback(_on_inbound_message)
        # Defer sync requests briefly so the RNS stack is fully ready
        QTimer.singleShot(_STARTUP_SYNC_DELAY_MS, self._sync_mgr.request_sync_all)
        # A peer announcing is what drives every other catch-up path, so
        # nothing covers the case where we are the node that was away.
        self._link_watcher = LinkWatcher(self._sync_mgr.request_sync_all)
        self._link_watcher.start()

        # Re-ask peers that never answered a sync request. Announces arrive in
        # a burst and then stop, so a request refused during one has nothing
        # else to trigger it again.
        self._sync_retry_timer = QTimer(self)
        self._sync_retry_timer.timeout.connect(self._sync_mgr.tick)
        self._sync_retry_timer.start(_SYNC_TICK_INTERVAL_MS)

        # Periodically prune stale presence entries and refresh the online panel
        self._presence_timer = QTimer(self)
        self._presence_timer.timeout.connect(self._on_presence_tick)
        self._presence_timer.start(_PRESENCE_PRUNE_INTERVAL_MS)

        # Network map auto-refresh timer — only runs while the map tab is visible
        self._network_map_timer = QTimer(self)
        self._network_map_timer.timeout.connect(self._refresh_network_map)

        # Debounce timer for announce-triggered map refreshes.
        # Announces arrive in bursts (one per destination per peer); we wait a
        # short window after the last one so the RNS path table has time to
        # populate before we read it.
        self._map_debounce_timer = QTimer(self)
        self._map_debounce_timer.setSingleShot(True)
        self._map_debounce_timer.timeout.connect(self._refresh_network_map_if_visible)

        # Debounce timer for announce-triggered re-announces.  When we receive
        # any trenchchat announce it means the network path is live, so we
        # re-announce ourselves on the same interface.  Debounced so a burst of
        # channel announces from one peer only triggers a single re-announce.
        # _pending_reannounce_iface holds the most recently seen interface; if
        # multiple interfaces fire before the debounce expires we fall back to
        # None (broadcast) since we can't target both simultaneously.
        self._pending_reannounce_iface = None
        self._reannounce_debounce_timer = QTimer(self)
        self._reannounce_debounce_timer.setSingleShot(True)
        self._reannounce_debounce_timer.timeout.connect(self._on_reannounce_debounced)
        self._reannounce_requested.connect(self._on_reannounce_requested)

        self._refresh_channel_list()
        self._restore_channel_selection()

    # --- UI construction ---

    def _build_ui(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        new_channel_action = QAction("＋ New Channel", self)
        new_channel_action.triggered.connect(self._on_new_channel)
        toolbar.addAction(new_channel_action)

        new_server_action = QAction("＋ New Server", self)
        new_server_action.triggered.connect(self._on_new_server)
        toolbar.addAction(new_server_action)

        join_channel_action = QAction("⤵ Join Channel", self)
        join_channel_action.triggered.connect(self._on_join_channel)
        toolbar.addAction(join_channel_action)

        toolbar.addSeparator()

        self._identity_label = QLabel()
        self._identity_label.setTextFormat(Qt.TextFormat.RichText)
        self._update_identity_label()
        toolbar.addWidget(self._identity_label)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        settings_action = QAction("⚙ Settings", self)
        settings_action.triggered.connect(self._on_settings)
        toolbar.addAction(settings_action)

        # Invite notification bar (hidden until an invite arrives)
        self._invite_bar = self._build_invite_bar()

        # Central widget wraps the bar + splitter
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._invite_bar)
        self.setCentralWidget(central)

        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        central_layout.addWidget(splitter, 1)

        # Left: channel list
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        ch_header = QLabel("  Channels")
        ch_header.setStyleSheet("font-weight: bold; padding: 8px 4px; color: #aaa;")
        left_layout.addWidget(ch_header)

        self._channel_list_widget = QListWidget()
        self._channel_list_widget.setStyleSheet(
            "QListWidget { border: none; background: #1a1a1a; }"
            "QListWidget::item { padding: 8px 12px; color: #ccc; }"
            "QListWidget::item:selected { background: #2a4a7a; color: #fff; }"
        )
        self._channel_list_widget.currentItemChanged.connect(self._on_channel_selected)
        self._channel_list_widget.itemClicked.connect(self._on_channel_list_clicked)
        self._channel_list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._channel_list_widget.customContextMenuRequested.connect(self._on_channel_context_menu)
        left_layout.addWidget(self._channel_list_widget)

        # Online users panel
        self._online_panel_expanded = True
        self._online_header = QLabel("  ▾ Online")
        self._online_header.setStyleSheet(
            "font-weight: bold; padding: 6px 4px 4px 4px; color: #aaa;"
        )
        self._online_header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._online_header.mousePressEvent = self._on_online_header_clicked
        left_layout.addWidget(self._online_header)

        self._online_list = QListWidget()
        self._online_list.setStyleSheet(
            "QListWidget { border: none; background: #1a1a1a; }"
            "QListWidget::item { padding: 4px 12px; color: #ccc; font-size: 12px; }"
            "QListWidget::item:selected { background: transparent; }"
        )
        self._online_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._online_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._online_list.setMaximumHeight(160)
        left_layout.addWidget(self._online_list)

        left.setMinimumWidth(180)
        left.setMaximumWidth(260)
        splitter.addWidget(left)

        # Right: tab widget — Chat tab and Network Map tab
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # --- Chat tab ---
        chat_tab = QWidget()
        chat_layout = QVBoxLayout(chat_tab)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        self._stack = QStackedWidget()
        placeholder = QLabel("Select a channel to start chatting")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet("color: #555; font-size: 16px;")
        self._stack.addWidget(placeholder)
        chat_layout.addWidget(self._stack, 1)

        self._compose = ComposeWidget(storage=self._storage)
        self._compose.message_ready.connect(self._on_send_message)
        self._compose.set_enabled(False)
        chat_layout.addWidget(self._compose)

        self._tabs.addTab(chat_tab, "💬 Chat")

        # --- Network Map tab ---
        map_tab = QWidget()
        map_tab_layout = QVBoxLayout(map_tab)
        map_tab_layout.setContentsMargins(0, 0, 0, 0)
        map_tab_layout.setSpacing(0)

        self._network_map_widget = NetworkMapWidget(self_hex=self._identity.hash_hex)
        map_tab_layout.addWidget(self._network_map_widget, 1)

        # Bottom bar: legend + refresh button
        map_bar = QWidget()
        map_bar.setStyleSheet("background: #111; border-top: 1px solid #333;")
        map_bar_layout = QHBoxLayout(map_bar)
        map_bar_layout.setContentsMargins(8, 4, 8, 4)

        map_legend = QLabel(
            "★ This device   ◆ Interface/Hub   ■ Transport node   ● Known peer   ○ Unknown"
            "      "
            "<span style='color:#3ddc3d'>━</span> Excellent  "
            "<span style='color:#e8e83a'>━</span> Good  "
            "<span style='color:#e8963a'>━</span> Fair  "
            "<span style='color:#e83a3a'>━</span> Poor"
        )
        map_legend.setTextFormat(Qt.TextFormat.RichText)
        map_legend.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
        map_bar_layout.addWidget(map_legend, 1)

        self._map_tc_only_check = QCheckBox("TrenchChat Network only")
        self._map_tc_only_check.setStyleSheet(
            "QCheckBox { color: #aaa; font-size: 11px; background: transparent; }"
            "QCheckBox::indicator { width: 13px; height: 13px; }"
        )
        self._map_tc_only_check.setToolTip(
            "When checked, only nodes that have been seen on the TrenchChat network "
            "(peers from your channels) are shown. Interface and transport nodes are "
            "always visible."
        )
        self._map_tc_only_check.toggled.connect(self._on_map_tc_only_toggled)
        self._map_tc_only_check.setChecked(True)
        map_bar_layout.addWidget(self._map_tc_only_check)

        self._map_refresh_btn = QPushButton("↻ Refresh")
        self._map_refresh_btn.setFixedWidth(80)
        self._map_refresh_btn.setStyleSheet(
            "QPushButton { background: #2a2a2a; color: #aaa; border: 1px solid #444;"
            " border-radius: 3px; padding: 2px 6px; font-size: 11px; }"
            "QPushButton:hover { background: #3a3a3a; }"
        )
        self._map_refresh_btn.clicked.connect(self._on_map_refresh_clicked)
        map_bar_layout.addWidget(self._map_refresh_btn)

        map_tab_layout.addWidget(map_bar)

        self._tabs.addTab(map_tab, "⬡ Network Map")

        # --- Interfaces tab ---
        self._interfaces_widget = InterfacesWidget(self._rns)
        self._tabs.addTab(self._interfaces_widget, "⚙ Interfaces")

        splitter.addWidget(self._tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    def _build_invite_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFrameShape(QFrame.Shape.NoFrame)
        bar.setStyleSheet(
            "QFrame { background: #2d4a1e; border-bottom: 1px solid #4a7a30; }"
            "QLabel { color: #b8e08a; font-size: 12px; background: transparent; border: none; }"
            "QPushButton { padding: 2px 10px; font-size: 11px; }"
        )
        bar.hide()

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 5, 10, 5)

        self._invite_bar_label = QLabel()
        layout.addWidget(self._invite_bar_label, 1)

        accept_btn = QPushButton("Accept")
        accept_btn.setStyleSheet("background: #3a8a20; color: white; border-radius: 3px;")
        accept_btn.clicked.connect(self._on_accept_invite)
        layout.addWidget(accept_btn)

        decline_btn = QPushButton("Decline")
        decline_btn.setStyleSheet("background: #5a2020; color: white; border-radius: 3px;")
        decline_btn.clicked.connect(self._on_decline_invite)
        layout.addWidget(decline_btn)

        next_btn = QPushButton("▸")
        next_btn.setToolTip("Next invite")
        next_btn.setFixedWidth(28)
        next_btn.setStyleSheet("background: #444; color: #ccc; border-radius: 3px;")
        next_btn.clicked.connect(self._on_next_invite)
        layout.addWidget(next_btn)

        return bar

    def _update_invite_bar(self):
        if not self._pending_invites:
            self._invite_bar.hide()
            return
        channel_hash, channel_name, token, expiry, admin_hex = self._pending_invites[0]
        count = len(self._pending_invites)
        count_str = f" ({count})" if count > 1 else ""
        if self._invite_mgr.invite_scope_kind(channel_hash) == "server":
            target = f"the server {channel_name} and all its channels"
        else:
            target = f"#{channel_name}"
        if token is None:
            # A member list doc held for confirmation, not a token invite.
            self._invite_bar_label.setText(
                f"📨  {admin_hex[:16]}… added you to  {target}{count_str}  "
                f"— join?"
            )
        else:
            self._invite_bar_label.setText(
                f"📨  You've been invited to join  {target}{count_str}  "
                f"— from {admin_hex[:16]}…"
            )
        self._invite_bar.show()

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #d4d4d4;
            }
            QToolBar {
                background: #252526;
                border-bottom: 1px solid #333;
                spacing: 4px;
                padding: 2px 4px;
            }
            QToolBar QToolButton {
                color: #ccc;
                padding: 4px 8px;
                border-radius: 4px;
            }
            QToolBar QToolButton:hover { background: #3a3a3a; }
            QSplitter::handle { background: #333; width: 1px; }
            QTextEdit, QLineEdit {
                background: #2d2d2d;
                color: #d4d4d4;
                border: 1px solid #444;
                border-radius: 4px;
            }
            QPushButton {
                background: #0e639c;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 4px 12px;
            }
            QPushButton:hover { background: #1177bb; }
            QPushButton:disabled { background: #333; color: #666; }
        """)

    # --- channel list ---

    def _add_channel_item(self, row, indented: bool = False):
        perms = permissions_from_json(row["permissions"])
        lock = " 🔒" if not is_open_join(perms) else ""
        prefix = "    # " if indented else "# "
        sync_mark = " ⟳" if row["hash"] in self._channel_sync_state else ""
        item = QListWidgetItem(f"{prefix}{row['name']}{lock}{sync_mark}")
        item.setData(Qt.ItemDataRole.UserRole, row["hash"])
        self._channel_list_widget.addItem(item)

    def _add_server_header(self, server, child_count: int):
        """A clickable but non-selectable header row that collapses its channels."""
        collapsed = server["hash"] in self._collapsed_servers
        caret = "▸" if collapsed else "▾"
        # Collapsing hides the channels but must not hide that they exist.
        suffix = f"  ({child_count})" if collapsed and child_count else ""
        item = QListWidgetItem(f"{caret} {server['name'].upper()}{suffix}")
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        # No UserRole hash: _highlight_channel_in_list and
        # _restore_channel_selection match on it, so headers stay invisible
        # to channel selection.
        item.setData(SERVER_HASH_ROLE, server["hash"])
        # Enabled so itemClicked fires, but not selectable, so clicking a
        # header never moves the current-channel selection.
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._channel_list_widget.addItem(item)

    @pyqtSlot(QListWidgetItem)
    def _on_channel_list_clicked(self, item: QListWidgetItem):
        server_hash = item.data(SERVER_HASH_ROLE)
        if not server_hash:
            return
        if server_hash in self._collapsed_servers:
            self._collapsed_servers.discard(server_hash)
        else:
            self._collapsed_servers.add(server_hash)
        self._settings.setValue("collapsed_servers",
                                sorted(self._collapsed_servers))
        self._refresh_channel_list()

    def _refresh_channel_list(self):
        # Suppress selection-change signals while rebuilding the list so we
        # don't trigger a spurious channel switch on clear().
        self._channel_list_widget.blockSignals(True)
        self._channel_list_widget.clear()

        if self._server_mgr is not None:
            for server in self._server_mgr.list_servers():
                children = [r for r in self._storage.get_server_channels(server["hash"])
                            if self._storage.is_subscribed(r["hash"])]
                self._add_server_header(server, len(children))
                if server["hash"] in self._collapsed_servers:
                    continue
                for row in children:
                    self._add_channel_item(row, indented=True)

        for row in self._storage.get_standalone_channels():
            if self._storage.is_subscribed(row["hash"]):
                self._add_channel_item(row)
        self._channel_list_widget.blockSignals(False)

        # Re-highlight whichever channel is currently open (if still in list).
        if self._current_channel:
            self._highlight_channel_in_list(self._current_channel)

    def _highlight_channel_in_list(self, channel_hash_hex: str):
        """Select the list row for channel_hash_hex without triggering a switch."""
        self._channel_list_widget.blockSignals(True)
        for i in range(self._channel_list_widget.count()):
            item = self._channel_list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == channel_hash_hex:
                self._channel_list_widget.setCurrentItem(item)
                break
        self._channel_list_widget.blockSignals(False)

    def _restore_channel_selection(self):
        """On startup: open the last channel the user had open."""
        last_channel = self._settings.value("last_channel")
        if not last_channel:
            return
        for i in range(self._channel_list_widget.count()):
            item = self._channel_list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == last_channel:
                # Allow signals so _on_channel_selected fires and the view is built.
                self._channel_list_widget.setCurrentItem(item)
                return

    # --- channel selection ---

    @pyqtSlot(QListWidgetItem, QListWidgetItem)
    def _on_channel_selected(self, current, previous):
        if current is None:
            return
        channel_hash = current.data(Qt.ItemDataRole.UserRole)
        self._switch_to_channel(channel_hash)

    def _switch_to_channel(self, channel_hash_hex: str):
        # Persist the last-read position for the channel we're leaving.
        if self._current_channel and self._current_channel != channel_hash_hex:
            last_msg = self._storage.get_latest_message_id(self._current_channel)
            if last_msg:
                self._settings.setValue(f"last_read/{self._current_channel}", last_msg)

        self._settings.setValue("last_channel", channel_hash_hex)
        self._current_channel = channel_hash_hex

        if channel_hash_hex not in self._channel_views:
            # Retrieve the scroll restore point saved from a previous session.
            restore_id = self._settings.value(f"last_read/{channel_hash_hex}") or None
            view = ChannelView(channel_hash_hex, self._storage,
                               self._identity.hash_hex,
                               restore_to_id=restore_id,
                               config=self._config,
                               reaction_mgr=self._reaction_mgr)
            view.react_requested.connect(self._on_react_requested)
            view.reaction_remove_requested.connect(self._on_reaction_remove_requested)
            self._channel_views[channel_hash_hex] = view
            self._stack.addWidget(view)

        current_view = self._channel_views[channel_hash_hex]
        current_view.set_sync_status(self._sync_mgr.status.get_status(channel_hash_hex))
        self._stack.setCurrentWidget(current_view)

        channel = self._storage.get_channel(channel_hash_hex)
        if channel:
            perms = permissions_from_json(channel["permissions"])
            if not is_open_join(perms):
                is_member = self._storage.is_member(channel_hash_hex, self._identity.hash_hex)
                can_send = is_member and self._storage.has_permission(
                    channel_hash_hex, self._identity.hash_hex, SEND_MESSAGE
                )
                self._compose.set_enabled(can_send)
                if not is_member:
                    self._compose.set_placeholder("You are not a member of this channel")
                elif not can_send:
                    self._compose.set_placeholder("You do not have permission to send messages")
                else:
                    self._compose.set_placeholder(f"Message #{channel['name']}…  (Enter to send)")
            else:
                self._compose.set_enabled(True)
                self._compose.set_placeholder(f"Message #{channel['name']}…  (Enter to send)")

        self._refresh_online_panel()

    # --- new / join channel ---

    @pyqtSlot()
    def _on_join_channel(self):
        dlg = JoinChannelDialog(self._storage, self._channel_mgr, self._router, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        channel_hash = dlg.selected_channel_hash
        if not channel_hash:
            return
        actions.join_public_channel(self._storage, self._subscription_mgr, channel_hash)
        self._refresh_channel_list()
        self._switch_to_channel(channel_hash)

    def _on_channel_discovered(self, channel_hash_hex: str, channel_name: str):
        """Called from background announce thread — marshal to main thread."""
        self._channel_discovered.emit(channel_hash_hex, channel_name)

    @pyqtSlot(str, str)
    def _on_channel_discovered_main_thread(self, channel_hash_hex: str, channel_name: str):
        """A new public channel was heard on the network — show a subtle notification."""
        self.statusBar().showMessage(
            f"New channel discovered: #{channel_name} — click 'Join Channel' to subscribe",
            8000,
        )

    @pyqtSlot()
    def _on_new_channel(self):
        dlg = NewChannelDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.channel_name
        if not name:
            QMessageBox.warning(self, "TrenchChat", "Channel name cannot be empty.")
            return
        try:
            hash_hex = actions.create_channel(
                self._channel_mgr, self._invite_mgr,
                name=name, description=dlg.description, permissions=dlg.permissions,
            )
        except NameInUseError as e:
            QMessageBox.warning(self, "TrenchChat", str(e).capitalize() + ".")
            return
        self._refresh_channel_list()
        self._switch_to_channel(hash_hex)

    def _on_new_server(self):
        dlg = NewServerDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not dlg.server_name:
            QMessageBox.warning(self, "TrenchChat", "Server name cannot be empty.")
            return
        try:
            actions.create_server(
                self._server_mgr, self._invite_mgr,
                name=dlg.server_name, description=dlg.description,
            )
        except NameInUseError as e:
            QMessageBox.warning(self, "TrenchChat", str(e).capitalize() + ".")
            return
        self._refresh_channel_list()

    def _on_new_channel_in_server(self, server_hash: str):
        dlg = NewChannelDialog(self, in_server=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not dlg.channel_name:
            QMessageBox.warning(self, "TrenchChat", "Channel name cannot be empty.")
            return
        try:
            hash_hex = actions.create_channel_in_server(
                self._storage, self._channel_mgr, self._invite_mgr,
                server_hash, self._identity.hash_hex,
                name=dlg.channel_name, description=dlg.description,
            )
        except NameInUseError as e:
            QMessageBox.warning(self, "TrenchChat", str(e).capitalize() + ".")
            return
        if hash_hex is None:
            QMessageBox.warning(
                self, "TrenchChat",
                "You don't have permission to create channels in this server.",
            )
            return
        self._refresh_channel_list()
        self._switch_to_channel(hash_hex)

    def _on_leave_server(self, server_hash: str):
        server = self._storage.get_server(server_hash)
        name = server["name"] if server else server_hash[:12]
        confirm = QMessageBox.question(
            self, "Leave server",
            f"Leave {name}? You'll leave every channel in it. "
            "Your local message history will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        for row in self._storage.get_server_channels(server_hash):
            view = self._channel_views.pop(row["hash"], None)
            if view is not None:
                view.setParent(None)
            if self._current_channel == row["hash"]:
                self._current_channel = None
        actions.leave_server(self._storage, self._subscription_mgr, server_hash,
                             self._identity.hash_hex)
        self._refresh_channel_list()

    # --- send ---

    @pyqtSlot(str, object)
    def _on_send_message(self, text: str, raw_image: object):
        if not self._current_channel:
            return

        image_data: bytes | None = None
        if raw_image:
            try:
                image_data, _ = prepare_image(bytes(raw_image))
            except Exception as exc:
                # The re-encode is the only sanitisation here, so a rejected
                # image must not be forwarded instead.
                RNS.log(f"TrenchChat: image rejected, not sent: {exc}", RNS.LOG_WARNING)
                image_data = None

        sent = actions.send_message(
            self._storage, self._subscription_mgr, self._messaging,
            self._current_channel, self._identity.hash_hex, text,
            image_data=image_data,
        )
        if not sent:
            return
        # Refresh our own view immediately (message was stored locally in send_message)
        if self._current_channel in self._channel_views:
            msg_id = self._storage.get_latest_message_id(self._current_channel)
            if msg_id:
                self._channel_views[self._current_channel].on_new_message(msg_id)

    # --- incoming message ---

    def _on_new_message(self, channel_hash_hex: str, message_id: str):
        """Called from LXMF background thread — marshal to main thread via signal."""
        self._message_received.emit(channel_hash_hex, message_id)

    @pyqtSlot(str, str)
    def _on_new_message_main_thread(self, channel_hash_hex: str, message_id: str):
        if channel_hash_hex in self._channel_views:
            self._channel_views[channel_hash_hex].on_new_message(message_id)
        else:
            self._refresh_channel_list()

    def _on_channel_joined(self, channel_hash_hex: str, channel_name: str):
        """Called from background thread when auto-joined a channel via invite."""
        self._channel_joined.emit(channel_hash_hex, channel_name)

    @pyqtSlot(str, str)
    def _on_channel_joined_main_thread(self, channel_hash_hex: str, channel_name: str):
        """Runs on the Qt main thread after a channel-joined event."""
        self._refresh_channel_list()

    def _on_member_list_updated(self, channel_hash_hex: str):
        """Called from background thread when a member list is accepted."""
        self._member_list_updated.emit(channel_hash_hex)

    @pyqtSlot(str)
    def _on_member_list_updated_main_thread(self, channel_hash_hex: str):
        # If the current channel's membership changed, refresh the compose state.
        if channel_hash_hex == self._current_channel:
            self._switch_to_channel(channel_hash_hex)
        self._refresh_channel_list()

    # --- channel context menu ---

    @pyqtSlot(QPoint)
    def _on_channel_context_menu(self, pos: QPoint):
        item = self._channel_list_widget.itemAt(pos)
        if item is None:
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #2d2d2d; color: #d4d4d4; border: 1px solid #444; }"
            "QMenu::item:selected { background: #2a4a7a; }"
            "QMenu::separator { background: #444; height: 1px; margin: 2px 0; }"
        )

        server_hash = item.data(SERVER_HASH_ROLE)
        if server_hash:
            self._build_server_menu(menu, server_hash)
            menu.exec(self._channel_list_widget.mapToGlobal(pos))
            return

        channel_hash = item.data(Qt.ItemDataRole.UserRole)
        channel = self._storage.get_channel(channel_hash)
        if channel is None:
            return

        my_hex = self._identity.hash_hex
        kind, scope_hash, _scope_name = self._scope_for(channel_hash)
        can_invite = self._storage.has_permission(scope_hash, my_hex, INVITE)
        can_manage_channel = self._storage.has_permission(scope_hash, my_hex, MANAGE_CHANNEL)
        is_member = self._storage.is_member(scope_hash, my_hex)
        in_server = kind == "server"

        if can_invite:
            label = "Invite to server…" if in_server else "Invite member…"
            invite_action = menu.addAction(label)
            invite_action.triggered.connect(
                lambda: self._on_invite_member(channel_hash, channel["name"])
            )

        if is_member:
            label = "View server members…" if in_server else "View members…"
            members_action = menu.addAction(label)
            members_action.triggered.connect(
                lambda: self._on_view_members(channel_hash, channel["name"])
            )

        if can_manage_channel:
            label = "Edit server permissions…" if in_server else "Edit permissions…"
            perms_action = menu.addAction(label)
            perms_action.triggered.connect(
                lambda: self._on_edit_permissions(channel_hash, channel["name"])
            )

        if menu.actions():
            menu.addSeparator()

        if in_server:
            # Membership is server-wide, so there is no such thing as leaving a
            # single channel of a server.
            leave_action = menu.addAction("Leave server")
            leave_action.triggered.connect(lambda: self._on_leave_server(scope_hash))
        else:
            leave_action = menu.addAction("Leave channel")
            leave_action.triggered.connect(lambda: self._on_leave_channel(channel_hash))

        menu.exec(self._channel_list_widget.mapToGlobal(pos))

    def _build_server_menu(self, menu: QMenu, server_hash: str):
        my_hex = self._identity.hash_hex
        server = self._storage.get_server(server_hash)
        if server is None:
            return
        name = server["name"]

        if self._storage.has_permission(server_hash, my_hex, CREATE_CHANNEL):
            action = menu.addAction("New channel in server…")
            action.triggered.connect(lambda: self._on_new_channel_in_server(server_hash))
        if self._storage.has_permission(server_hash, my_hex, INVITE):
            action = menu.addAction("Invite to server…")
            action.triggered.connect(lambda: self._on_invite_to_server(server_hash, name))
        if self._storage.is_member(server_hash, my_hex):
            action = menu.addAction("View server members…")
            action.triggered.connect(lambda: self._on_view_server_members(server_hash, name))
        if self._storage.has_permission(server_hash, my_hex, MANAGE_CHANNEL):
            action = menu.addAction("Edit server permissions…")
            action.triggered.connect(lambda: self._on_edit_server_permissions(server_hash, name))

        if menu.actions():
            menu.addSeparator()
        leave = menu.addAction("Leave server")
        leave.triggered.connect(lambda: self._on_leave_server(server_hash))

    def _on_invite_member(self, channel_hash: str, channel_name: str):
        # An invite to a server admits the peer to every channel in it, so the
        # invite is addressed to the server rather than the channel clicked.
        kind, scope_hash, scope_name = self._scope_for(channel_hash)
        self._invite_to_scope(kind, scope_hash, scope_name)

    def _on_invite_to_server(self, server_hash: str, server_name: str):
        self._invite_to_scope("server", server_hash, server_name)

    def _invite_to_scope(self, kind: str, scope_hash: str, scope_name: str):
        dlg = InviteDialog(scope_name, self._user_directory, self._storage, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        invitee_hex = dlg.invitee_hash
        if invitee_hex:
            self._invite_mgr.send_invite(scope_hash, invitee_hex)
            extra = (" They will join every channel in this server."
                     if kind == "server" else "")
            QMessageBox.information(
                self, "Invite sent",
                f"Invite sent to {invitee_hex[:16]}…\n"
                f"They will be added once they accept.{extra}"
            )

    def _on_view_members(self, channel_hash: str, channel_name: str):
        # Membership belongs to the server for a channel inside one. Storage
        # resolves this anyway, but addressing the scope explicitly is what
        # keeps the permissions path above and this one from drifting apart.
        _kind, scope_hash, scope_name = self._scope_for(channel_hash)
        self._view_members_for_scope(scope_hash, scope_name)

    def _on_view_server_members(self, server_hash: str, server_name: str):
        self._view_members_for_scope(server_hash, server_name)

    def _view_members_for_scope(self, scope_hash: str, scope_name: str):
        dlg = MembersDialog(
            scope_hash, scope_name, self._storage,
            self._identity.hash_hex,
            self._storage.is_admin(scope_hash, self._identity.hash_hex),
            self,
            config=self._config,
            user_directory=self._user_directory,
        )
        dlg.exec()
        actions.update_membership(
            self._storage, self._invite_mgr, scope_hash, self._identity.hash_hex,
            remove_members=dlg.members_to_remove,
            add_admins=dlg.admins_to_add,
            remove_admins=dlg.admins_to_remove,
        )

    def _scope_for(self, channel_hash: str) -> tuple[str, str, str]:
        """The scope that owns a channel's membership and permissions.

        Returns (kind, hash, display_name) where kind is "server" or "channel".
        Permissions for a channel inside a server live on the server, so editing
        them through the channel would write a row the next accepted document
        overwrites.
        """
        row = self._storage.get_channel(channel_hash)
        if row is not None and row["server_hash"]:
            server = self._storage.get_server(row["server_hash"])
            name = server["name"] if server else row["server_hash"][:12]
            return ("server", row["server_hash"], name)
        name = row["name"] if row is not None else channel_hash[:12]
        return ("channel", channel_hash, name)

    def _on_edit_permissions(self, channel_hash: str, channel_name: str):
        kind, scope_hash, scope_name = self._scope_for(channel_hash)
        self._edit_permissions_for_scope(kind, scope_hash, scope_name, channel_hash)

    def _on_edit_server_permissions(self, server_hash: str, server_name: str):
        self._edit_permissions_for_scope("server", server_hash, server_name, None)

    def _edit_permissions_for_scope(self, kind: str, scope_hash: str,
                                    scope_name: str, channel_hash: str | None):
        if not self._storage.has_permission(scope_hash, self._identity.hash_hex,
                                            MANAGE_CHANNEL):
            return
        if kind == "server":
            current_perms = self._storage.get_server_permissions(scope_hash)
        else:
            current_perms = self._storage.get_channel_permissions(scope_hash)
        dlg = ChannelPermissionsDialog(scope_name, current_perms, self,
                                       scope_kind=kind)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if kind == "server":
            actions.edit_server_permissions(
                self._storage, self._invite_mgr, scope_hash,
                self._identity.hash_hex, dlg.permissions,
            )
        else:
            actions.edit_channel_permissions(
                self._storage, self._invite_mgr, scope_hash,
                self._identity.hash_hex, dlg.permissions,
            )
        self._refresh_channel_list()
        if channel_hash and self._current_channel == channel_hash:
            self._switch_to_channel(channel_hash)

    def _on_leave_channel(self, channel_hash: str):
        channel = self._storage.get_channel(channel_hash)
        name = channel["name"] if channel else channel_hash[:12]
        confirm = QMessageBox.question(
            self, "Leave channel",
            f"Leave #{name}? Your local message history will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            actions.leave_channel(self._storage, self._subscription_mgr, channel_hash)
            if channel_hash in self._channel_views:
                view = self._channel_views.pop(channel_hash)
                self._stack.removeWidget(view)
                view.deleteLater()
            self._current_channel = None
            self._compose.set_enabled(False)
            self._refresh_channel_list()

    # --- online users panel ---

    def _on_online_header_clicked(self, _event) -> None:
        """Toggle the online users list visibility."""
        self._online_panel_expanded = not self._online_panel_expanded
        if self._online_panel_expanded:
            self._online_list.show()
            self._online_header.setText("  ▾ Online")
        else:
            self._online_list.hide()
            self._online_header.setText("  ▸ Online")

    def _refresh_online_panel(self) -> None:
        """Repopulate the online users list for the currently selected channel."""
        if self._current_channel is None:
            self._online_list.clear()
            self._online_header.setText("  ▾ Online")
            return

        entries = self._presence_mgr.get_online_for_channel(
            self._current_channel,
            self._storage,
            self._subscription_mgr,
        )

        online_count = sum(1 for e in entries if e["is_online"])
        self._online_header.setText(
            f"  {'▾' if self._online_panel_expanded else '▸'} "
            f"Online ({online_count})"
        )

        self._online_list.clear()
        for entry in entries:
            dot = "● " if entry["is_online"] else "○ "
            color = "#4ec94e" if entry["is_online"] else "#666"
            item = QListWidgetItem(dot + entry["display_name"])
            item.setForeground(QColor(color))
            self._online_list.addItem(item)

    def _on_presence_changed(self, peer_hex: str, is_online: bool) -> None:
        """Called from RNS background thread — marshal to main thread."""
        self._presence_changed.emit(peer_hex, is_online)

    @pyqtSlot(str, bool)
    def _on_presence_changed_main_thread(self, peer_hex: str, is_online: bool) -> None:
        """Refresh the online panel when any peer's status changes."""
        self._refresh_online_panel()

    @pyqtSlot(str)
    def _on_avatar_updated_main_thread(self, identity_hex: str) -> None:
        """Refresh channel views so updated avatars are reflected immediately."""
        for view in self._channel_views.values():
            view.refresh_avatars(identity_hex)

    @pyqtSlot(str, str)
    def _on_reaction_updated_main_thread(self, channel_hash_hex: str,
                                         message_id: str) -> None:
        """Refresh the reaction bar for a specific message."""
        view = self._channel_views.get(channel_hash_hex)
        if view is not None:
            view.on_reaction_updated(message_id)

    @pyqtSlot(str)
    def _on_emoji_received_main_thread(self, emoji_hash: str) -> None:
        """A new custom emoji image just arrived -- reload all open channel views.

        Any message that contained an unknown :name: token will now be able to
        render it as an inline image on the next load.  The easiest correct
        approach is to call load_history() on every visible channel view so
        the messages are rebuilt with the newly available emoji data.
        """
        for view in self._channel_views.values():
            view.load_history()

    @pyqtSlot(str)
    @pyqtSlot(str)
    def _on_sync_status_changed_main_thread(self, channel_hash_hex: str) -> None:
        """Update the sidebar indicator, and the channel view if it's visible."""
        status = self._sync_mgr.status.get_status(channel_hash_hex)
        state = status["state"]
        if state in (SyncState.SYNCING.value, SyncState.INCOMPLETE.value):
            self._channel_sync_state[channel_hash_hex] = state
        else:
            self._channel_sync_state.pop(channel_hash_hex, None)
        self._refresh_channel_list()

        if channel_hash_hex == self._current_channel:
            view = self._channel_views.get(channel_hash_hex)
            if view is not None:
                view.set_sync_status(status)

    @pyqtSlot(str, str)
    def _on_react_requested(self, channel_hash_hex: str, message_id: str) -> None:
        """User clicked the react button -- show the EmojiPicker popup."""
        if self._reaction_mgr is None:
            return
        picker = EmojiPicker(self._storage, self)
        picker.emoji_selected.connect(
            lambda emoji_hash, ch=channel_hash_hex, mid=message_id:
                self._do_add_reaction(ch, mid, emoji_hash)
        )
        picker.focus_search()
        # Position near the cursor
        from PyQt6.QtGui import QCursor
        picker.move(QCursor.pos())
        picker.show()

    def _get_reaction_peers(self, channel_hash_hex: str) -> list[str]:
        """Return the list of peers to broadcast a reaction to.

        Delegates to actions.compute_channel_recipients -- the same
        recipient logic _on_send_message uses (minus the SEND_MESSAGE
        gate, which doesn't apply to reactions).
        """
        return actions.compute_channel_recipients(
            self._storage, self._subscription_mgr, channel_hash_hex,
            self._identity.hash_hex,
        )

    def _do_add_reaction(self, channel_hash_hex: str, message_id: str,
                         emoji_hash: str) -> None:
        """Send the add-reaction command via ReactionManager."""
        if self._reaction_mgr is None:
            return
        self._reaction_mgr.add_reaction(
            channel_hash_hex, message_id, emoji_hash,
            self._get_reaction_peers(channel_hash_hex),
        )

    @pyqtSlot(str, str, str)
    def _on_reaction_remove_requested(self, channel_hash_hex: str, message_id: str,
                                      emoji_hash: str) -> None:
        """User clicked a reaction chip they already reacted with -- remove it."""
        if self._reaction_mgr is None:
            return
        self._reaction_mgr.remove_reaction(
            channel_hash_hex, message_id, emoji_hash,
            self._get_reaction_peers(channel_hash_hex),
        )

    def _on_presence_tick(self) -> None:
        """Periodic timer: prune stale presence, directory and sync state, refresh the panel."""
        self._presence_mgr.prune()
        self._user_directory.prune()
        self._sync_mgr.status.prune()
        if self._presence_beacon is not None:
            self._presence_beacon.tick()
        if self._reaction_mgr is not None:
            self._reaction_mgr.retry_pending_emoji()
        self._refresh_online_panel()

    def _seed_user_directory(self, peer_hex: str) -> None:
        """Add a peer to the user directory if they are a known TrenchChat user.

        Called from lxmf.delivery announces and inbound messages — signals that
        do not inherently confirm a peer as TrenchChat.  We treat a peer as
        confirmed TrenchChat if they are already in the directory (from a prior
        trenchchat.user announce) or if they appear in any channel's members
        table (added via a signed member list update).  In either case we
        resolve the best available display name and refresh their entry.
        """
        if self._user_directory.contains(peer_hex):
            display_name = resolve_display_name(
                peer_hex, self._identity.hash_hex, self._storage, self._config
            )
            self._user_directory.record_user(peer_hex, display_name)
            return
        if peer_hex in self._storage.get_trenchchat_peer_identities():
            display_name = resolve_display_name(
                peer_hex, self._identity.hash_hex, self._storage, self._config
            )
            self._user_directory.record_user(peer_hex, display_name)

    def _on_reannounce_requested(self, iface) -> None:
        """Slot called on the main thread when an announce is received.

        Updates the pending interface and (re)starts the debounce timer.  If
        two different interfaces fire before the timer expires we fall back to
        None (broadcast) since we can't target both simultaneously.
        """
        if self._reannounce_debounce_timer.isActive():
            # Second (or later) trigger within the debounce window — if the
            # interface differs from what we already have, clear it so we
            # broadcast rather than pick one arbitrarily.
            if iface is not self._pending_reannounce_iface:
                self._pending_reannounce_iface = None
        else:
            self._pending_reannounce_iface = iface
        self._reannounce_debounce_timer.start(_ANNOUNCE_DEBOUNCE_MS)

    def _on_reannounce_debounced(self) -> None:
        """Re-announce after receiving a trenchchat announce from a peer.

        Receiving any trenchchat announce means the network path to the hub is
        live.  Re-announcing on the same interface ensures the peer hears us
        even if our startup announce was sent before the interface was ready,
        without spamming unrelated interfaces.
        """
        iface, self._pending_reannounce_iface = self._pending_reannounce_iface, None
        self._router.announce(attached_interface=iface)
        self._router.announce_user(attached_interface=iface)
        self._channel_mgr.announce_all_owned(attached_interface=iface)
        if iface is not None:
            RNS.log(
                f"TrenchChat: re-announced on {iface} after peer announce",
                RNS.LOG_DEBUG,
            )
        else:
            RNS.log(
                "TrenchChat: re-announced on all interfaces after peer announce",
                RNS.LOG_DEBUG,
            )

    # --- incoming invite ---

    def _on_incoming_invite(self, channel_hash_hex: str, channel_name: str,
                             token: bytes, expiry: float, admin_hash_hex: str):
        # Called from LXMF background thread — emit signal to cross to main thread.
        self._invite_received.emit(channel_hash_hex, channel_name, token, expiry, admin_hash_hex)

    @pyqtSlot(str, str, bytes, float, str)
    def _on_invite_received_main_thread(self, channel_hash_hex: str, channel_name: str,
                                         token: bytes, expiry: float, admin_hash_hex: str):
        # A re-invite to the same channel refreshes the pending entry (new
        # token/expiry) instead of stacking a second one alongside it.
        self._pending_invites = [
            inv for inv in self._pending_invites if inv[0] != channel_hash_hex
        ]
        self._pending_invites.append((channel_hash_hex, channel_name, token, expiry, admin_hash_hex))
        self._update_invite_bar()

    @pyqtSlot()
    def _on_accept_invite(self):
        if not self._pending_invites:
            return
        channel_hash, channel_name, token, expiry, admin_hex = self._pending_invites.pop(0)

        # token is None when an admin added us directly: the member list
        # document is already held, so confirming applies it rather than
        # sending a join request.
        if token is None:
            if self._invite_mgr.accept_pending_membership(channel_hash):
                QMessageBox.information(
                    self, "Channel joined",
                    f"You've joined #{channel_name}."
                )
            else:
                QMessageBox.warning(
                    self, "Could not join",
                    f"The membership record for #{channel_name} could not be "
                    "verified, so nothing was applied."
                )
            self._update_invite_bar()
            return

        is_server = self._invite_mgr.invite_scope_kind(channel_hash) == "server"
        self._invite_mgr.send_join_request(channel_hash, token, expiry, admin_hex)
        target = f"the server {channel_name}" if is_server else f"#{channel_name}"
        QMessageBox.information(
            self, "Join request sent",
            f"Your request to join {target} has been sent.\n"
            "You'll be added once an admin approves it."
        )
        self._update_invite_bar()

    @pyqtSlot()
    def _on_decline_invite(self):
        if self._pending_invites:
            channel_hash, _name, token, _expiry, _admin = self._pending_invites.pop(0)
            if token is None:
                self._invite_mgr.decline_pending_membership(channel_hash)
            else:
                self._invite_mgr.decline_invite(channel_hash)
        self._update_invite_bar()

    @pyqtSlot()
    def _on_next_invite(self):
        if len(self._pending_invites) > 1:
            # Rotate to show the next pending invite
            self._pending_invites.append(self._pending_invites.pop(0))
        self._update_invite_bar()

    # --- network map tab ---

    _TAB_CHAT = 0
    _TAB_NETWORK_MAP = 1
    _TAB_INTERFACES = 2
    _NETWORK_MAP_REFRESH_MS = 10_000

    @pyqtSlot(int)
    def _on_tab_changed(self, index: int) -> None:
        """Start or stop refresh timers based on the active tab."""
        if self._network_map_timer is None:
            return
        if index == self._TAB_NETWORK_MAP:
            self._refresh_network_map()
            self._network_map_timer.start(self._NETWORK_MAP_REFRESH_MS)
        else:
            self._network_map_timer.stop()
            if self._map_debounce_timer is not None:
                self._map_debounce_timer.stop()

        if index == self._TAB_INTERFACES:
            self._interfaces_widget.start_refresh_timer()
        else:
            self._interfaces_widget.stop_refresh_timer()

    @pyqtSlot()
    def _on_map_refresh_clicked(self) -> None:
        """Manual refresh button — briefly disable to give visual feedback."""
        self._map_refresh_btn.setEnabled(False)
        self._map_refresh_btn.setText("…")
        self._refresh_network_map()
        QTimer.singleShot(800, lambda: (
            self._map_refresh_btn.setEnabled(True),
            self._map_refresh_btn.setText("↻ Refresh"),
        ))

    @pyqtSlot(bool)
    def _on_map_tc_only_toggled(self, checked: bool) -> None:
        """Apply or remove the TrenchChat-peers-only filter on the network map."""
        if checked:
            self._network_map_widget.set_peer_filter(
                self._storage.get_trenchchat_peer_identities()
            )
        else:
            self._network_map_widget.set_peer_filter(None)

    # Delay between the last announce and the resulting map refresh.
    # Long enough for RNS to populate path-table entries after a burst of
    # announces, short enough to feel responsive.
    _MAP_ANNOUNCE_DEBOUNCE_MS = 2_000

    def _schedule_map_refresh(self) -> None:
        """Restart the debounce timer on every announce.

        The actual refresh fires _MAP_ANNOUNCE_DEBOUNCE_MS after the *last*
        announce in a burst, by which time the RNS path table is populated.
        """
        if self._map_debounce_timer is None:
            return
        self._map_debounce_timer.start(self._MAP_ANNOUNCE_DEBOUNCE_MS)

    def _refresh_network_map_if_visible(self) -> None:
        """Refresh the map only when the Network Map tab is currently active."""
        if self._tabs.currentIndex() == self._TAB_NETWORK_MAP:
            self._refresh_network_map()

    @pyqtSlot()
    def _refresh_network_map(self) -> None:
        """Fetch current network topology and push it to the map widget."""
        data = gather_network_data(self._rns, self._identity.hash_hex,
                                   self._storage, self._user_directory)
        self._network_map_widget.set_data(data["nodes"], data["edges"])
        # Keep the peer filter up to date if it is currently active.
        if self._map_tc_only_check.isChecked():
            self._network_map_widget.set_peer_filter(
                self._storage.get_trenchchat_peer_identities()
            )

    # --- settings ---

    @pyqtSlot()
    def _update_identity_label(self) -> None:
        """Refresh the toolbar identity label to reflect the current display name."""
        self._identity_label.setText(
            f"  {self._config.display_name}  "
            f"<span style='color:#555;font-size:10px'>"
            f"{self._identity.hash_hex[:12]}…</span>"
        )

    def _on_settings(self):
        dlg = SettingsDialog(
            self._config, self._identity, self._storage, self._router,
            avatar_mgr=self._avatar_mgr,
            subscriber_lookup=self._subscription_mgr.get_subscribers,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Propagate the (possibly new) display name to all live components
            self._router.set_display_name(self._config.display_name)
            self._router.announce()
            self._update_identity_label()
            self._refresh_online_panel()
            self._refresh_channel_list()
