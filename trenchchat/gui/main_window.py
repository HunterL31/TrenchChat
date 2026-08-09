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
    QListWidget, QListWidgetItem, QSplitter,
    QLabel, QDialog, QLineEdit, QDialogButtonBox,
    QMessageBox, QStackedWidget, QMenu,
    QPushButton, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QTabWidget, QCheckBox,
)
from PyQt6.QtCore import Qt, pyqtSlot, QPoint, QPointF, QSize, pyqtSignal, QTimer, QSettings
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from trenchchat.gui import theme
from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.image import prepare_image, MAX_IMAGE_BYTES
from trenchchat.core.permissions import (
    INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES, SEND_MESSAGE, PRESETS, PRESET_PRIVATE,
    DEFAULT_PRESET, is_discoverable, is_open_join, permissions_from_json,
)
from trenchchat.core.presence import PresenceManager, resolve_display_name
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.core.reaction import ReactionManager
from trenchchat.core.sync import SyncManager
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
_PRESENCE_PRUNE_INTERVAL_MS = 30_000
_ANNOUNCE_DEBOUNCE_MS = 2_000


def _make_solid_avatar_pixmap(letter: str, color_hex: str, size: int) -> QPixmap:
    """Return a size×size solid-color circle with a centered letter (top-bar identity avatar)."""
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    painter.fillPath(path, QColor(color_hex))
    font = painter.font()
    font.setPointSize(max(8, size // 2 - 1))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(theme.BG))
    painter.drawText(result.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return result


def _make_channel_icon_pixmap(color_hex: str, size: int = 13) -> QPixmap:
    """Return a small tilted-hash "channel" glyph, matching the sidebar icon in the design."""
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color_hex))
    pen.setWidthF(size * 0.11)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    s = size / 20.0
    painter.drawLine(QPointF(7 * s, 4 * s), QPointF(5 * s, 16 * s))
    painter.drawLine(QPointF(14 * s, 4 * s), QPointF(12 * s, 16 * s))
    painter.drawLine(QPointF(3.5 * s, 8 * s), QPointF(16 * s, 8 * s))
    painter.drawLine(QPointF(3 * s, 13 * s), QPointF(15.5 * s, 13 * s))
    painter.end()
    return result


class NewChannelDialog(QDialog):
    """Matches the "New channel" card from the main-window design: a name
    field, an optional description, and a Public / Invite-only segmented
    visibility toggle in place of the old preset dropdown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Channel")
        self.setFixedWidth(380)
        self.setStyleSheet(f"""
            QDialog {{ background: {theme.DIALOG_BG}; }}
            QLabel {{ color: {theme.TEXT_MUTED}; font-size: 12px; background: transparent; }}
            QLineEdit {{
                background: {theme.BG}; color: {theme.TEXT};
                border: 1px solid {theme.BORDER}; border-radius: 8px;
                padding: 7px 10px; font-size: 14px;
            }}
            QLineEdit:focus {{ border: 1px solid {theme.ACCENT}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)

        title = QLabel("New channel")
        title.setStyleSheet(f"font-size: 18px; font-weight: 500; color: {theme.TEXT};")
        layout.addWidget(title)

        layout.addWidget(QLabel("Name"))
        self._name = QLineEdit()
        self._name.setPlaceholderText("general")
        self._name.textChanged.connect(self._update_create_enabled)
        layout.addWidget(self._name)

        layout.addWidget(QLabel("Description (optional)"))
        self._desc = QLineEdit()
        layout.addWidget(self._desc)

        layout.addWidget(QLabel("Visibility"))
        self._public_btn = QPushButton("Public")
        self._private_btn = QPushButton("Invite-only")
        self._public_btn.setFixedHeight(32)
        self._private_btn.setFixedHeight(32)
        self._public_btn.clicked.connect(lambda: self._select_preset("open"))
        self._private_btn.clicked.connect(lambda: self._select_preset("private"))
        vis_wrap = QWidget()
        vis_wrap.setStyleSheet(
            f"background: transparent; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        vis_layout = QHBoxLayout(vis_wrap)
        vis_layout.setContentsMargins(0, 0, 0, 0)
        vis_layout.setSpacing(0)
        vis_layout.addWidget(self._public_btn)
        vis_layout.addWidget(self._private_btn)
        layout.addWidget(vis_wrap)

        self._preset = DEFAULT_PRESET
        self._apply_preset_styles()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {theme.TEXT};
                border: 1px solid {theme.BORDER_STRONG}; border-radius: 8px;
                padding: 0 14px; height: 34px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {theme.BORDER_SOFT}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        self._create_btn = QPushButton("Create")
        self._create_btn.setDefault(True)
        self._create_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {theme.ACCENT};
                border: 1px solid {theme.ACCENT}; border-radius: 8px;
                padding: 0 14px; height: 34px; font-size: 13px; font-weight: 500;
            }}
            QPushButton:hover {{ background: {theme.ACCENT_WASH_HOVER}; }}
            QPushButton:disabled {{ color: {theme.TEXT_FAINT}; border-color: {theme.BORDER}; }}
        """)
        self._create_btn.clicked.connect(self._on_create_clicked)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._create_btn)
        layout.addLayout(btn_row)

        self._update_create_enabled()

    def _select_preset(self, preset: str) -> None:
        self._preset = preset
        self._apply_preset_styles()

    def _apply_preset_styles(self) -> None:
        for btn, preset in ((self._public_btn, "open"), (self._private_btn, "private")):
            active = preset == self._preset
            border = "none" if preset == "open" else f"1px solid {theme.BORDER}"
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {theme.ACCENT_WASH_SELECTED if active else 'transparent'};
                    color: {theme.ACCENT if active else theme.TEXT};
                    border: none; border-left: {border if preset == 'private' else 'none'};
                    font-size: 13px; padding: 0;
                }}
                QPushButton:hover {{ background: {theme.ACCENT_WASH_SELECTED if active else theme.BORDER_SOFT}; }}
            """)

    def _update_create_enabled(self) -> None:
        self._create_btn.setEnabled(bool(self._name.text().strip()))

    def _on_create_clicked(self) -> None:
        if self._name.text().strip():
            self.accept()

    @property
    def channel_name(self) -> str:
        return self._name.text().strip()

    @property
    def description(self) -> str:
        return self._desc.text().strip()

    @property
    def permissions(self) -> dict:
        return dict(PRESETS.get(self._preset, PRESET_PRIVATE))


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

        self.setStyleSheet(f"""
            QDialog {{ background: {theme.DIALOG_BG}; }}
            QTableWidget {{
                background: {theme.BG}; color: {theme.TEXT};
                gridline-color: {theme.BORDER}; border: 1px solid {theme.BORDER};
                border-radius: 8px;
            }}
            QTableWidget::item:selected {{ background: {theme.ACCENT_WASH_SELECTED}; color: {theme.ACCENT}; }}
            QHeaderView::section {{
                background: {theme.PANEL_BG}; color: {theme.TEXT_MUTED};
                border: none; border-bottom: 1px solid {theme.BORDER}; padding: 4px 6px;
            }}
        """)

        layout = QVBoxLayout(self)

        hint = QLabel("Channels announced on the network appear here. "
                      "Click Refresh to request fresh announcements.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px; padding: 4px;")
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

    def __init__(self, config: Config, identity: Identity, storage: Storage,
                 rns: "RNS.Reticulum", router: Router, channel_mgr: ChannelManager,
                 messaging: Messaging, subscription_mgr: SubscriptionManager,
                 invite_mgr: InviteManager, presence_mgr: PresenceManager,
                 user_directory: UserDirectory, avatar_mgr=None, reaction_mgr=None):
        super().__init__()
        self._config = config
        self._identity = identity
        self._storage = storage
        self._rns = rns
        self._router = router
        self._channel_mgr = channel_mgr
        self._messaging = messaging
        self._subscription_mgr = subscription_mgr
        self._invite_mgr = invite_mgr
        self._presence_mgr = presence_mgr
        self._user_directory = user_directory
        self._avatar_mgr = avatar_mgr
        self._reaction_mgr: ReactionManager | None = reaction_mgr

        # Pending invites: list of (channel_hash_hex, channel_name, token, expiry, admin_hash_hex)
        self._pending_invites: list[tuple] = []

        self._channel_views: dict[str, ChannelView] = {}
        self._current_channel: str | None = None
        self._settings = QSettings("TrenchChat", "TrenchChat")

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

        self._avatar_updated.connect(self._on_avatar_updated_main_thread)
        if avatar_mgr is not None:
            avatar_mgr.add_avatar_callback(self._avatar_updated.emit)

        self._reaction_updated.connect(self._on_reaction_updated_main_thread)
        self._emoji_received.connect(self._on_emoji_received_main_thread)
        if reaction_mgr is not None:
            reaction_mgr.add_reaction_callback(self._reaction_updated.emit)
            reaction_mgr.add_emoji_callback(self._emoji_received.emit)

        self._sync_mgr = SyncManager(
            identity, storage, router, messaging, subscription_mgr, invite_mgr
        )

        def _on_peer_appeared(peer_hex: str, iface) -> None:
            self._sync_mgr.on_peer_appeared(peer_hex)
            self._presence_mgr.record_seen(peer_hex)
            self._seed_user_directory(peer_hex)
            if self._avatar_mgr is not None:
                self._avatar_mgr.flush_avatar(peer_hex)
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

        # Also mark a peer as seen when we receive any inbound LXMF message from
        # them.  This covers the case where a peer connects via a backchannel link
        # and sends a message without having announced their delivery destination
        # first (which is the normal LXMF direct-delivery flow).
        def _on_inbound_message(message: "LXMF.LXMessage") -> None:
            if not message.source_hash:
                return
            sender_identity = RNS.Identity.recall(message.source_hash)
            if sender_identity is not None:
                sender_hex = sender_identity.hash.hex()
                self._presence_mgr.record_seen(sender_hex)
                self._seed_user_directory(sender_hex)

        router.add_delivery_callback(_on_inbound_message)
        # Defer sync requests briefly so the RNS stack is fully ready
        QTimer.singleShot(_STARTUP_SYNC_DELAY_MS, self._sync_mgr.request_sync_all)

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
        # Central widget wraps the top bar + invite bar + splitter
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._build_top_bar())

        # Invite notification bar (hidden until an invite arrives)
        self._invite_bar = self._build_invite_bar()
        central_layout.addWidget(self._invite_bar)
        self.setCentralWidget(central)

        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet(f"QSplitter::handle {{ background: {theme.DIVIDER}; width: 1px; }}")
        central_layout.addWidget(splitter, 1)

        # Left: channel list
        left = QWidget()
        left.setStyleSheet(f"background: {theme.SIDEBAR_BG}; border-right: 1px solid {theme.DIVIDER};")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        ch_header_row = QWidget()
        ch_header_layout = QHBoxLayout(ch_header_row)
        ch_header_layout.setContentsMargins(16, 14, 12, 6)
        ch_header = QLabel("CHANNELS")
        ch_header.setStyleSheet(
            f"font-size: 11px; font-weight: 600; letter-spacing: 1px; color: {theme.TEXT_FAINT};"
        )
        ch_header_layout.addWidget(ch_header, 1)
        ch_add_btn = QPushButton("＋")
        ch_add_btn.setToolTip("New channel")
        ch_add_btn.setFixedSize(20, 20)
        ch_add_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {theme.TEXT_FAINT}; border: none;
                font-size: 13px; padding: 0;
            }}
            QPushButton:hover {{ color: {theme.TEXT}; }}
        """)
        ch_add_btn.clicked.connect(self._on_new_channel)
        ch_header_layout.addWidget(ch_add_btn)
        left_layout.addWidget(ch_header_row)

        self._channel_list_widget = QListWidget()
        self._channel_list_widget.setIconSize(QSize(13, 13))
        self._channel_list_widget.setSpacing(0)
        self._channel_list_widget.setStyleSheet(f"""
            QListWidget {{ border: none; background: {theme.SIDEBAR_BG}; outline: none; }}
            QListWidget::item {{
                padding: 7px 10px; margin: 1px 8px; border-radius: 8px;
                color: {theme.TEXT}; font-size: 13.5px;
            }}
            QListWidget::item:selected {{ background: {theme.ACCENT_WASH_SELECTED}; color: {theme.ACCENT}; }}
            QListWidget::item:hover:!selected {{ background: {theme.BORDER_SOFT}; }}
        """)
        self._channel_list_widget.currentItemChanged.connect(self._on_channel_selected)
        self._channel_list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._channel_list_widget.customContextMenuRequested.connect(self._on_channel_context_menu)
        left_layout.addWidget(self._channel_list_widget)

        # Faint fading divider between the channel list and the online panel
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"""
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 transparent, stop:0.2 {theme.DIVIDER},
                stop:0.8 {theme.DIVIDER}, stop:1 transparent
            );
            border: none; margin: 0 16px;
        """)
        divider_wrap = QWidget()
        divider_wrap_layout = QVBoxLayout(divider_wrap)
        divider_wrap_layout.setContentsMargins(16, 12, 16, 0)
        divider_wrap_layout.addWidget(divider)
        left_layout.addWidget(divider_wrap)

        # Online users panel
        self._online_panel_expanded = True
        online_header_row = QWidget()
        online_header_row.setCursor(Qt.CursorShape.PointingHandCursor)
        online_header_row.mousePressEvent = self._on_online_header_clicked
        online_header_layout = QHBoxLayout(online_header_row)
        online_header_layout.setContentsMargins(16, 10, 12, 6)
        self._online_header = QLabel("ONLINE — 0")
        self._online_header.setStyleSheet(
            f"font-size: 11px; font-weight: 600; letter-spacing: 1px; color: {theme.TEXT_FAINT};"
        )
        online_header_layout.addWidget(self._online_header, 1)
        self._online_chevron = QLabel("▼")
        self._online_chevron.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px;")
        online_header_layout.addWidget(self._online_chevron)
        left_layout.addWidget(online_header_row)

        self._online_list = QListWidget()
        self._online_list.setStyleSheet(f"""
            QListWidget {{ border: none; background: {theme.SIDEBAR_BG}; outline: none; }}
            QListWidget::item {{ padding: 0; }}
            QListWidget::item:selected {{ background: transparent; }}
        """)
        self._online_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._online_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._online_list.setMaximumHeight(160)
        left_layout.addWidget(self._online_list)
        left_layout.addStretch()

        left.setMinimumWidth(200)
        left.setMaximumWidth(280)
        splitter.addWidget(left)

        # Right: tab widget — Chat tab and Network Map tab
        self._tabs = QTabWidget()
        self._tabs.setObjectName("mainTabs")
        self._tabs.setDocumentMode(True)
        self._tabs.setStyleSheet(f"""
            QTabWidget#mainTabs::pane {{ border: none; background: {theme.BG}; }}
            QTabWidget#mainTabs::tab-bar {{ left: 16px; top: 6px; }}
            QTabBar::tab {{
                background: transparent; color: {theme.TEXT_MUTED};
                padding: 7px 14px; font-size: 13px;
                border: 1px solid {theme.BORDER}; border-right: none;
            }}
            QTabBar::tab:first {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; }}
            QTabBar::tab:last {{
                border-right: 1px solid {theme.BORDER};
                border-top-right-radius: 8px; border-bottom-right-radius: 8px;
            }}
            QTabBar::tab:selected {{ color: {theme.ACCENT}; background: {theme.ACCENT_WASH_HOVER}; }}
            QTabBar::tab:hover:!selected {{ color: {theme.TEXT}; }}
        """)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # --- Chat tab ---
        chat_tab = QWidget()
        chat_layout = QVBoxLayout(chat_tab)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        self._stack = QStackedWidget()
        placeholder = QLabel("Select a channel to start chatting")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 15px;")
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
        map_bar.setStyleSheet(f"background: {theme.PANEL_BG}; border-top: 1px solid {theme.DIVIDER};")
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
        map_legend.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px; background: transparent;")
        map_bar_layout.addWidget(map_legend, 1)

        self._map_tc_only_check = QCheckBox("TrenchChat Network only")
        self._map_tc_only_check.setStyleSheet(f"""
            QCheckBox {{ color: {theme.TEXT_MUTED}; font-size: 11px; background: transparent; }}
            QCheckBox::indicator {{ width: 13px; height: 13px; }}
        """)
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
        self._map_refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {theme.TEXT_MUTED}; border: 1px solid {theme.BORDER};
                border-radius: 6px; padding: 2px 6px; font-size: 11px;
            }}
            QPushButton:hover {{ background: {theme.BORDER_SOFT}; }}
        """)
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

    def _icon_btn_qss(self) -> str:
        """Shared style for the borderless icon buttons in the top bar.

        padding: 0 is required — without it these fixed-size square buttons
        inherit the global QPushButton rule's `padding: 5px 14px`, which on a
        32x32 button leaves almost no room for the glyph and clips it down to
        a sliver.
        """
        return f"""
            QPushButton {{
                background: transparent; color: {theme.TEXT_MUTED};
                border: 1px solid transparent; border-radius: 8px; font-size: 14px;
                padding: 0;
            }}
            QPushButton:hover {{ background: {theme.BORDER_SOFT}; color: {theme.TEXT}; }}
        """

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setStyleSheet(f"""
            QFrame#topBar {{ background: {theme.BG}; border-bottom: 1px solid {theme.DIVIDER}; }}
            QFrame#topBar QLabel {{ background: transparent; }}
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 9, 16, 9)
        layout.setSpacing(10)

        brand = QLabel(
            f"📡&nbsp;&nbsp;<span style='font-size:16px;font-weight:500;color:{theme.TEXT}'>"
            f"TrenchChat</span>"
        )
        brand.setTextFormat(Qt.TextFormat.RichText)
        brand.setStyleSheet(f"color: {theme.ACCENT}; font-size: 16px;")
        layout.addWidget(brand)
        layout.addStretch(1)

        search_btn = QPushButton("🔍")
        search_btn.setToolTip("Browse and join channels")
        search_btn.setFixedSize(32, 32)
        search_btn.setStyleSheet(self._icon_btn_qss())
        search_btn.clicked.connect(self._on_join_channel)
        layout.addWidget(search_btn)

        new_channel_btn = QPushButton("＋  New channel")
        new_channel_btn.setFixedHeight(32)
        new_channel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {theme.ACCENT};
                border: 1px solid {theme.ACCENT}; border-radius: 8px;
                padding: 0 12px; font-weight: 500; font-size: 13px;
            }}
            QPushButton:hover {{ background: {theme.ACCENT_WASH_HOVER}; }}
        """)
        new_channel_btn.clicked.connect(self._on_new_channel)
        layout.addWidget(new_channel_btn)

        identity_row = QWidget()
        identity_layout = QHBoxLayout(identity_row)
        identity_layout.setContentsMargins(6, 0, 6, 0)
        identity_layout.setSpacing(8)
        self._identity_avatar = QLabel()
        self._identity_avatar.setFixedSize(26, 26)
        identity_layout.addWidget(self._identity_avatar)
        self._identity_hash_label = QLabel()
        self._identity_hash_label.setStyleSheet(
            f"font-size: 11px; font-family: {theme.MONO_FONT_FAMILY}; color: {theme.TEXT_FAINT};"
        )
        identity_layout.addWidget(self._identity_hash_label)
        layout.addWidget(identity_row)

        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Settings")
        settings_btn.setFixedSize(32, 32)
        settings_btn.setStyleSheet(self._icon_btn_qss())
        settings_btn.clicked.connect(self._on_settings)
        layout.addWidget(settings_btn)

        self._update_identity_label()
        return bar

    def _build_invite_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFrameShape(QFrame.Shape.NoFrame)
        bar.setStyleSheet(f"""
            QFrame {{ background: {theme.INVITE_BG}; border-bottom: 1px solid {theme.INVITE_BORDER}; }}
            QLabel {{ color: {theme.INVITE_TEXT}; font-size: 13px; background: transparent; border: none; }}
        """)
        bar.hide()

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)

        self._invite_bar_label = QLabel()
        self._invite_bar_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._invite_bar_label, 1)

        accept_btn = QPushButton("Accept")
        accept_btn.setFixedHeight(26)
        accept_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {theme.ACCENT}; border: 1px solid {theme.ACCENT};
                border-radius: 6px; padding: 0 12px; font-size: 12px; font-weight: 500;
            }}
            QPushButton:hover {{ background: {theme.ACCENT_WASH_HOVER}; }}
        """)
        accept_btn.clicked.connect(self._on_accept_invite)
        layout.addWidget(accept_btn)

        decline_btn = QPushButton("Decline")
        decline_btn.setFixedHeight(26)
        decline_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {theme.TEXT}; border: 1px solid {theme.BORDER_STRONG};
                border-radius: 6px; padding: 0 12px; font-size: 12px;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.08); }}
        """)
        decline_btn.clicked.connect(self._on_decline_invite)
        layout.addWidget(decline_btn)

        next_btn = QPushButton("▸")
        next_btn.setToolTip("Next invite")
        next_btn.setFixedSize(24, 24)
        next_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: #a7a1db; border: none;
                border-radius: 6px; padding: 0;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.08); }}
        """)
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
        self._invite_bar_label.setText(
            f"You've been invited to join <strong>#{channel_name}</strong>{count_str} "
            f"by {admin_hex[:12]}…"
        )
        self._invite_bar.show()

    def _apply_dark_theme(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {theme.BG};
                color: {theme.TEXT};
                font-family: {theme.FONT_FAMILY};
            }}
            QSplitter::handle {{ background: {theme.DIVIDER}; width: 1px; }}
            QTextEdit, QLineEdit {{
                background: {theme.INPUT_BG};
                color: {theme.TEXT};
                border: 1px solid {theme.BORDER};
                border-radius: 8px;
                padding: 5px 8px;
            }}
            QTextEdit:focus, QLineEdit:focus {{ border: 1px solid {theme.ACCENT}; }}
            QPushButton {{
                background: transparent;
                color: {theme.TEXT};
                border: 1px solid {theme.BORDER_STRONG};
                border-radius: 8px;
                padding: 5px 14px;
            }}
            QPushButton:hover {{ background: {theme.BORDER_SOFT}; }}
            QPushButton:disabled {{ color: {theme.TEXT_FAINT}; border-color: {theme.BORDER}; }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
            QScrollBar::handle:vertical {{
                background: {theme.BORDER_STRONG}; border-radius: 5px; min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {theme.TEXT_MUTED}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

    # --- channel list ---

    def _refresh_channel_list(self):
        # Suppress selection-change signals while rebuilding the list so we
        # don't trigger a spurious channel switch on clear().
        self._channel_list_widget.blockSignals(True)
        self._channel_list_widget.clear()
        for row in self._storage.get_all_channels():
            if not self._storage.is_subscribed(row["hash"]):
                continue
            perms = permissions_from_json(row["permissions"])
            lock = "  🔒" if not is_open_join(perms) else ""
            item = QListWidgetItem(f"{row['name']}{lock}")
            item.setData(Qt.ItemDataRole.UserRole, row["hash"])
            self._channel_list_widget.addItem(item)
        self._channel_list_widget.blockSignals(False)

        self._update_channel_icons()
        # Re-highlight whichever channel is currently open (if still in list).
        if self._current_channel:
            self._highlight_channel_in_list(self._current_channel)

    def _update_channel_icons(self) -> None:
        """Recolor each channel row's leading icon: accent when selected, muted otherwise."""
        muted_icon = QIcon(_make_channel_icon_pixmap(theme.TEXT_MUTED))
        accent_icon = QIcon(_make_channel_icon_pixmap(theme.ACCENT))
        for i in range(self._channel_list_widget.count()):
            item = self._channel_list_widget.item(i)
            is_selected = item.data(Qt.ItemDataRole.UserRole) == self._current_channel
            item.setIcon(accent_icon if is_selected else muted_icon)

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
        self._update_channel_icons()

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

        self._stack.setCurrentWidget(self._channel_views[channel_hash_hex])

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
        channel = self._storage.get_channel(channel_hash)
        owner_hash = channel["creator_hash"] if channel else None
        self._subscription_mgr.subscribe(channel_hash, owner_hash)
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
        hash_hex = self._channel_mgr.create_channel(
            name=name,
            description=dlg.description,
            permissions=dlg.permissions,
        )
        if not is_open_join(dlg.permissions):
            self._invite_mgr.publish_member_list(hash_hex)
        self._refresh_channel_list()
        self._switch_to_channel(hash_hex)

    # --- send ---

    @pyqtSlot(str, object)
    def _on_send_message(self, text: str, raw_image: object):
        if not self._current_channel:
            return

        channel = self._storage.get_channel(self._current_channel)
        perms = permissions_from_json(channel["permissions"]) if channel else {}
        if channel and not is_open_join(perms):
            if not self._storage.has_permission(
                self._current_channel, self._identity.hash_hex, SEND_MESSAGE
            ):
                return
            all_dests = [
                row["identity_hash"]
                for row in self._storage.get_members(self._current_channel)
            ]
        else:
            subs = self._subscription_mgr.get_subscribers(self._current_channel)
            all_dests = list(subs) if subs else []
            # Always include self so the message is stored locally even with no subscribers.
            if self._identity.hash_hex not in all_dests:
                all_dests.append(self._identity.hash_hex)

        image_data: bytes | None = None
        if raw_image:
            try:
                image_data, _ = prepare_image(bytes(raw_image))
            except Exception as exc:
                RNS.log(f"TrenchChat: image preparation failed: {exc}", RNS.LOG_WARNING)
                if len(bytes(raw_image)) <= MAX_IMAGE_BYTES:
                    image_data = bytes(raw_image)

        self._messaging.send_message(
            channel_hash_hex=self._current_channel,
            content=text,
            subscriber_hashes=all_dests,
            image_data=image_data,
        )
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
        channel_hash = item.data(Qt.ItemDataRole.UserRole)
        channel = self._storage.get_channel(channel_hash)
        if channel is None:
            return

        my_hex = self._identity.hash_hex
        can_invite = self._storage.has_permission(channel_hash, my_hex, INVITE)
        can_manage_channel = self._storage.has_permission(channel_hash, my_hex, MANAGE_CHANNEL)
        is_member = self._storage.is_member(channel_hash, my_hex)

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {theme.DIALOG_BG}; color: {theme.TEXT};
                border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 4px;
            }}
            QMenu::item {{ padding: 6px 14px; border-radius: 6px; }}
            QMenu::item:selected {{ background: {theme.ACCENT_WASH_SELECTED}; color: {theme.ACCENT}; }}
            QMenu::separator {{ background: {theme.DIVIDER}; height: 1px; margin: 4px 4px; }}
        """)

        if can_invite:
            invite_action = menu.addAction("Invite member…")
            invite_action.triggered.connect(
                lambda: self._on_invite_member(channel_hash, channel["name"])
            )

        if is_member:
            members_action = menu.addAction("View members…")
            members_action.triggered.connect(
                lambda: self._on_view_members(channel_hash, channel["name"])
            )

        if can_manage_channel:
            perms_action = menu.addAction("Edit permissions…")
            perms_action.triggered.connect(
                lambda: self._on_edit_permissions(channel_hash, channel["name"])
            )

        if menu.actions():
            menu.addSeparator()

        leave_action = menu.addAction("Leave channel")
        leave_action.triggered.connect(lambda: self._on_leave_channel(channel_hash))

        menu.exec(self._channel_list_widget.mapToGlobal(pos))

    def _on_invite_member(self, channel_hash: str, channel_name: str):
        dlg = InviteDialog(channel_name, self._user_directory, self._storage, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        invitee_hex = dlg.invitee_hash
        if invitee_hex:
            self._invite_mgr.send_invite(channel_hash, invitee_hex)
            QMessageBox.information(
                self, "Invite sent",
                f"Invite sent to {invitee_hex[:16]}…\n"
                "They will be added once they accept."
            )

    def _on_view_members(self, channel_hash: str, channel_name: str):
        dlg = MembersDialog(
            channel_hash, channel_name, self._storage,
            self._identity.hash_hex,
            self._storage.is_admin(channel_hash, self._identity.hash_hex),
            self,
        )
        dlg.exec()
        my_hex = self._identity.hash_hex
        can_kick = self._storage.has_permission(channel_hash, my_hex, KICK)
        can_manage_roles = self._storage.has_permission(channel_hash, my_hex, MANAGE_ROLES)
        remove_members = [m for m in dlg.members_to_remove] if can_kick else []
        add_admins = [a for a in dlg.admins_to_add] if can_manage_roles else []
        remove_admins = [a for a in dlg.admins_to_remove] if can_manage_roles else []
        if remove_members or add_admins or remove_admins:
            self._invite_mgr.publish_member_list(
                channel_hash,
                remove_members=remove_members or None,
                add_admins=add_admins or None,
                remove_admins=remove_admins or None,
            )

    def _on_edit_permissions(self, channel_hash: str, channel_name: str):
        if not self._storage.has_permission(channel_hash, self._identity.hash_hex, MANAGE_CHANNEL):
            return
        current_perms = self._storage.get_channel_permissions(channel_hash)
        dlg = ChannelPermissionsDialog(channel_name, current_perms, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_perms = dlg.permissions
        self._storage.set_channel_permissions(channel_hash, new_perms)
        self._invite_mgr.broadcast_permissions(channel_hash)
        self._refresh_channel_list()
        if self._current_channel == channel_hash:
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
            owner_hash = channel["creator_hash"] if channel else None
            self._subscription_mgr.unsubscribe(channel_hash, owner_hash)
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
        self._online_list.setVisible(self._online_panel_expanded)
        self._online_chevron.setText("▼" if self._online_panel_expanded else "▶")

    def _make_online_row_widget(self, display_name: str, hash_hex: str,
                                is_online: bool) -> QWidget:
        """Build one online-panel row: coloured presence dot + name + trailing hash."""
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(8)

        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot_color = theme.ONLINE_DOT if is_online else theme.OFFLINE_DOT
        dot.setStyleSheet(f"background: {dot_color}; border-radius: 3px;")
        layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

        name = QLabel(display_name)
        name.setStyleSheet(f"color: {theme.rgba(theme.TEXT, 0.85)}; font-size: 12.5px;")
        layout.addWidget(name, 1)

        hash_label = QLabel(hash_hex[:8])
        hash_label.setStyleSheet(
            f"color: {theme.TEXT_SUBTLE}; font-size: 10.5px; font-family: {theme.MONO_FONT_FAMILY};"
        )
        layout.addWidget(hash_label, 0, Qt.AlignmentFlag.AlignRight)
        return row

    def _refresh_online_panel(self) -> None:
        """Repopulate the online users list for the currently selected channel."""
        self._online_list.clear()
        if self._current_channel is None:
            self._online_header.setText("ONLINE — 0")
            return

        entries = self._presence_mgr.get_online_for_channel(
            self._current_channel,
            self._storage,
            self._subscription_mgr,
        )

        online_count = sum(1 for e in entries if e["is_online"])
        self._online_header.setText(f"ONLINE — {online_count}")

        for entry in entries:
            item = QListWidgetItem()
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            row_widget = self._make_online_row_widget(
                entry["display_name"], entry["identity_hash"], entry["is_online"]
            )
            item.setSizeHint(row_widget.sizeHint())
            self._online_list.addItem(item)
            self._online_list.setItemWidget(item, row_widget)

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

        Uses the same dual-path logic as _on_send_message: member table for
        invite-only channels, subscriber list for open-join channels.
        """
        channel = self._storage.get_channel(channel_hash_hex)
        perms = permissions_from_json(channel["permissions"]) if channel else {}
        if channel and not is_open_join(perms):
            return [
                row["identity_hash"]
                for row in self._storage.get_members(channel_hash_hex)
            ]
        subs = self._subscription_mgr.get_subscribers(channel_hash_hex)
        peers = list(subs) if subs else []
        if self._identity.hash_hex not in peers:
            peers.append(self._identity.hash_hex)
        return peers

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
        """Periodic timer: prune stale presence and user directory entries, refresh the panel."""
        self._presence_mgr.prune()
        self._user_directory.prune()
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
        self._pending_invites.append((channel_hash_hex, channel_name, token, expiry, admin_hash_hex))
        self._update_invite_bar()

    @pyqtSlot()
    def _on_accept_invite(self):
        if not self._pending_invites:
            return
        channel_hash, channel_name, token, expiry, admin_hex = self._pending_invites.pop(0)
        self._invite_mgr.send_join_request(channel_hash, token, expiry, admin_hex)
        QMessageBox.information(
            self, "Join request sent",
            f"Your request to join #{channel_name} has been sent.\n"
            "You'll be added once an admin approves it."
        )
        self._update_invite_bar()

    @pyqtSlot()
    def _on_decline_invite(self):
        if self._pending_invites:
            self._pending_invites.pop(0)
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
        data = gather_network_data(self._rns, self._identity.hash_hex, self._storage)
        self._network_map_widget.set_data(data["nodes"], data["edges"])
        # Keep the peer filter up to date if it is currently active.
        if self._map_tc_only_check.isChecked():
            self._network_map_widget.set_peer_filter(
                self._storage.get_trenchchat_peer_identities()
            )

    # --- settings ---

    @pyqtSlot()
    def _update_identity_label(self) -> None:
        """Refresh the top-bar avatar/hash to reflect the current identity/display name."""
        letter = (self._config.display_name[:1] or "?").upper()
        self._identity_avatar.setPixmap(_make_solid_avatar_pixmap(letter, theme.ACCENT, 26))
        self._identity_avatar.setToolTip(self._config.display_name)
        self._identity_hash_label.setText(self._identity.hash_hex[:8])
        self._identity_hash_label.setToolTip(self._identity.hash_hex)

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
