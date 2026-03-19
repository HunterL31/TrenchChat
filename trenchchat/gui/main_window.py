"""
Main application window.

Layout:
  ┌─────────────────────────────────────────────┐
  │  [+] New Channel   [Settings]               │  ← toolbar
  ├──────────────┬──────────────────────────────┤
  │              │                              │
  │  Channel     │   Message view               │
  │  list        │   (ChannelView)              │
  │              │                              │
  │              ├──────────────────────────────┤
  │              │   Compose (ComposeWidget)    │
  └──────────────┴──────────────────────────────┘
"""

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QSplitter, QToolBar,
    QLabel, QDialog, QFormLayout, QLineEdit, QComboBox,
    QDialogButtonBox, QMessageBox, QStackedWidget, QMenu,
    QPushButton, QFrame,
)
from PyQt6.QtCore import Qt, pyqtSlot, QPoint, pyqtSignal, QTimer
from PyQt6.QtGui import QAction, QFont

from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.network.router import Router
from trenchchat.gui.channel_view import ChannelView
from trenchchat.gui.compose import ComposeWidget
from trenchchat.gui.settings import SettingsDialog
from trenchchat.gui.invite_dialogs import InviteDialog, MembersDialog


class NewChannelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Channel")
        layout = QFormLayout(self)

        self._name = QLineEdit()
        self._name.setPlaceholderText("general")
        layout.addRow("Name:", self._name)

        self._desc = QLineEdit()
        layout.addRow("Description:", self._desc)

        self._access = QComboBox()
        self._access.addItems(["public", "invite"])
        layout.addRow("Access:", self._access)

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
    def access_mode(self) -> str:
        return self._access.currentText()


class MainWindow(QMainWindow):
    # Signals used to safely marshal background-thread events onto the Qt main thread.
    _invite_received  = pyqtSignal(str, str, bytes, float, str)
    _message_received = pyqtSignal(str, str)   # channel_hash_hex, message_id

    def __init__(self, config: Config, identity: Identity, storage: Storage,
                 router: Router, channel_mgr: ChannelManager,
                 messaging: Messaging, subscription_mgr: SubscriptionManager,
                 invite_mgr: InviteManager):
        super().__init__()
        self._config = config
        self._identity = identity
        self._storage = storage
        self._router = router
        self._channel_mgr = channel_mgr
        self._messaging = messaging
        self._subscription_mgr = subscription_mgr
        self._invite_mgr = invite_mgr

        # Pending invites: list of (channel_hash_hex, channel_name, token, expiry, admin_hash_hex)
        self._pending_invites: list[tuple] = []

        self._channel_views: dict[str, ChannelView] = {}
        self._current_channel: str | None = None

        self.setWindowTitle("TrenchChat")
        self.setMinimumSize(800, 600)
        self._apply_dark_theme()
        self._build_ui()

        # Connect thread-safe signals to main-thread handlers
        self._invite_received.connect(self._on_invite_received_main_thread)
        self._message_received.connect(self._on_new_message_main_thread)

        messaging.add_message_callback(self._on_new_message)
        invite_mgr.add_invite_callback(self._on_incoming_invite)
        invite_mgr.add_channel_joined_callback(self._on_channel_joined)
        self._refresh_channel_list()

    # --- UI construction ---

    def _build_ui(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        new_channel_action = QAction("＋ New Channel", self)
        new_channel_action.triggered.connect(self._on_new_channel)
        toolbar.addAction(new_channel_action)

        toolbar.addSeparator()

        identity_label = QLabel(
            f"  {self._config.display_name}  "
            f"<span style='color:#555;font-size:10px'>"
            f"{self._identity.hash_hex[:12]}…</span>"
        )
        identity_label.setTextFormat(Qt.TextFormat.RichText)
        toolbar.addWidget(identity_label)

        spacer = QWidget()
        spacer.setSizePolicy(
            spacer.sizePolicy().horizontalPolicy(),
            spacer.sizePolicy().verticalPolicy(),
        )
        from PyQt6.QtWidgets import QSizePolicy
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
        self._channel_list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._channel_list_widget.customContextMenuRequested.connect(self._on_channel_context_menu)
        left_layout.addWidget(self._channel_list_widget)
        left.setMinimumWidth(180)
        left.setMaximumWidth(260)
        splitter.addWidget(left)

        # Right: stacked message views + compose
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._stack = QStackedWidget()
        placeholder = QLabel("Select a channel to start chatting")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet("color: #555; font-size: 16px;")
        self._stack.addWidget(placeholder)
        right_layout.addWidget(self._stack, 1)

        self._compose = ComposeWidget()
        self._compose.message_ready.connect(self._on_send_message)
        self._compose.set_enabled(False)
        right_layout.addWidget(self._compose)

        splitter.addWidget(right)
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
        self._invite_bar_label.setText(
            f"📨  You've been invited to join  #{channel_name}{count_str}  "
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

    def _refresh_channel_list(self):
        self._channel_list_widget.clear()
        for row in self._storage.get_all_channels():
            if not self._storage.is_subscribed(row["hash"]):
                continue
            lock = " 🔒" if row["access_mode"] == "invite" else ""
            item = QListWidgetItem(f"# {row['name']}{lock}")
            item.setData(Qt.ItemDataRole.UserRole, row["hash"])
            self._channel_list_widget.addItem(item)

    # --- channel selection ---

    @pyqtSlot(QListWidgetItem, QListWidgetItem)
    def _on_channel_selected(self, current, previous):
        if current is None:
            return
        channel_hash = current.data(Qt.ItemDataRole.UserRole)
        self._switch_to_channel(channel_hash)

    def _switch_to_channel(self, channel_hash_hex: str):
        self._current_channel = channel_hash_hex

        if channel_hash_hex not in self._channel_views:
            view = ChannelView(channel_hash_hex, self._storage,
                               self._identity.hash_hex)
            self._channel_views[channel_hash_hex] = view
            self._stack.addWidget(view)

        self._stack.setCurrentWidget(self._channel_views[channel_hash_hex])
        self._compose.set_enabled(True)

        channel = self._storage.get_channel(channel_hash_hex)
        if channel:
            self._compose.set_placeholder(
                f"Message #{channel['name']}…  (Enter to send)"
            )

    # --- new channel ---

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
            access_mode=dlg.access_mode,
        )
        # For invite-only channels, initialise the member list with the creator
        # as the first admin. This also writes the creator into the members table
        # so is_admin() returns True and the Invite Member menu item appears.
        if dlg.access_mode == "invite":
            self._invite_mgr.publish_member_list(hash_hex)
        self._refresh_channel_list()
        self._switch_to_channel(hash_hex)

    # --- send ---

    @pyqtSlot(str)
    def _on_send_message(self, text: str):
        if not self._current_channel:
            return

        channel = self._storage.get_channel(self._current_channel)
        if channel and channel["access_mode"] == "invite":
            # For invite-only channels use the members table as the recipient list.
            all_dests = [
                row["identity_hash"]
                for row in self._storage.get_members(self._current_channel)
            ]
        else:
            subs = self._subscription_mgr.get_subscribers(self._current_channel)
            all_dests = list(subs) if subs else []

        self._messaging.send_message(
            channel_hash_hex=self._current_channel,
            content=text,
            subscriber_hashes=all_dests,
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
        QTimer.singleShot(0, self._refresh_channel_list)

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

        is_invite_channel = channel["access_mode"] == "invite"
        is_admin = self._storage.is_admin(channel_hash, self._identity.hash_hex)

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #2d2d2d; color: #d4d4d4; border: 1px solid #444; }"
            "QMenu::item:selected { background: #2a4a7a; }"
            "QMenu::separator { background: #444; height: 1px; margin: 2px 0; }"
        )

        if is_invite_channel and is_admin:
            invite_action = menu.addAction("Invite member…")
            invite_action.triggered.connect(
                lambda: self._on_invite_member(channel_hash, channel["name"])
            )

        if is_invite_channel:
            members_action = menu.addAction("View members…")
            members_action.triggered.connect(
                lambda: self._on_view_members(channel_hash, channel["name"], is_admin)
            )

        if menu.actions():
            menu.addSeparator()

        leave_action = menu.addAction("Leave channel")
        leave_action.triggered.connect(lambda: self._on_leave_channel(channel_hash))

        menu.exec(self._channel_list_widget.mapToGlobal(pos))

    def _on_invite_member(self, channel_hash: str, channel_name: str):
        dlg = InviteDialog(channel_name, self)
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

    def _on_view_members(self, channel_hash: str, channel_name: str, is_admin: bool):
        dlg = MembersDialog(
            channel_hash, channel_name, self._storage,
            self._identity.hash_hex, is_admin, self
        )
        dlg.exec()
        # Apply any pending membership changes
        if dlg.members_to_remove or dlg.admins_to_add or dlg.admins_to_remove:
            self._invite_mgr.publish_member_list(
                channel_hash,
                remove_members=dlg.members_to_remove or None,
                add_admins=dlg.admins_to_add or None,
                remove_admins=dlg.admins_to_remove or None,
            )

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

    # --- settings ---

    @pyqtSlot()
    def _on_settings(self):
        from trenchchat.network.router import Router as R
        dlg = SettingsDialog(
            self._config, self._identity, self._storage, self._router, self
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_channel_list()
