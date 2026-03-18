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
    QDialogButtonBox, QMessageBox, QStackedWidget,
)
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QAction, QFont

from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.network.router import Router
from trenchchat.gui.channel_view import ChannelView
from trenchchat.gui.compose import ComposeWidget
from trenchchat.gui.settings import SettingsDialog


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
    def __init__(self, config: Config, identity: Identity, storage: Storage,
                 router: Router, channel_mgr: ChannelManager,
                 messaging: Messaging, subscription_mgr: SubscriptionManager):
        super().__init__()
        self._config = config
        self._identity = identity
        self._storage = storage
        self._router = router
        self._channel_mgr = channel_mgr
        self._messaging = messaging
        self._subscription_mgr = subscription_mgr

        self._channel_views: dict[str, ChannelView] = {}
        self._current_channel: str | None = None

        self.setWindowTitle("TrenchChat")
        self.setMinimumSize(800, 600)
        self._apply_dark_theme()
        self._build_ui()

        messaging.add_message_callback(self._on_new_message)
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

        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

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
        self._refresh_channel_list()
        self._switch_to_channel(hash_hex)

    # --- send ---

    @pyqtSlot(str)
    def _on_send_message(self, text: str):
        if not self._current_channel:
            return
        subs = self._subscription_mgr.get_subscribers(self._current_channel)
        # Always include ourselves so the message is stored locally.
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
        if channel_hash_hex in self._channel_views:
            self._channel_views[channel_hash_hex].on_new_message(message_id)
        else:
            # Channel not currently open — just refresh the list to update last_seen
            self._refresh_channel_list()

    # --- settings ---

    @pyqtSlot()
    def _on_settings(self):
        from trenchchat.network.router import Router as R
        dlg = SettingsDialog(
            self._config, self._identity, self._storage, self._router, self
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_channel_list()
