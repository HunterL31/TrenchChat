"""
Invite-related dialogs:
  - InviteDialog              : admin sends an invite (searchable user picker + manual fallback)
  - MembersDialog             : view/remove members for a channel
  - IncomingInviteDialog      : invitee accepts or declines an invite
  - ChannelPermissionsDialog  : owner/admin edits per-role permissions and channel flags
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem,
    QDialogButtonBox, QMessageBox, QWidget, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from trenchchat.core.permissions import (
    ALL_PERMISSIONS, FLAG_DISCOVERABLE, FLAG_OPEN_JOIN,
    INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    SEND_MESSAGE,
)
from trenchchat.core.storage import Storage
from trenchchat.core.user_directory import UserDirectory

_PERMISSION_LABELS: dict[str, str] = {
    SEND_MESSAGE:   "Send messages",
    INVITE:         "Invite members",
    KICK:           "Remove members",
    MANAGE_ROLES:   "Manage roles",
    MANAGE_CHANNEL: "Manage channel settings",
    "speak":        "Speak in voice channel",
    "manage_relay": "Manage voice relay",
}


# ---------------------------------------------------------------------------
# InviteDialog — admin invites a member via searchable user picker or manual hash
# ---------------------------------------------------------------------------

class InviteDialog(QDialog):
    """Invite dialog with a searchable list of discovered TrenchChat users.

    Shows peers from the UserDirectory that have been seen on the network via
    trenchchat.user announces.  A manual hex-entry fallback is always available
    for peers whose announces have not yet been heard.
    """

    def __init__(self, channel_name: str, user_directory: UserDirectory,
                 storage: Storage, parent=None):
        super().__init__(parent)
        self._user_directory = user_directory
        self._storage = storage
        self._invitee_hash: str | None = None

        self.setWindowTitle(f"Invite to #{channel_name}")
        self.setMinimumWidth(460)
        self.setMinimumHeight(380)

        layout = QVBoxLayout(self)

        # --- search section ---
        search_label = QLabel("Search for a TrenchChat user:")
        search_label.setStyleSheet("font-weight: bold; margin-bottom: 2px;")
        layout.addWidget(search_label)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Type a name or hash to filter…")
        self._search_edit.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search_edit)

        self._user_list = QListWidget()
        self._user_list.setStyleSheet(
            "QListWidget { background: #1e1e1e; border: 1px solid #444; }"
            "QListWidget::item { padding: 6px 8px; color: #ccc; }"
            "QListWidget::item:selected { background: #2a4a7a; }"
        )
        self._user_list.setMinimumHeight(160)
        self._user_list.currentItemChanged.connect(self._on_list_selection_changed)
        self._user_list.itemDoubleClicked.connect(self._on_list_double_clicked)
        layout.addWidget(self._user_list)

        no_peers_hint = QLabel(
            "No TrenchChat users discovered yet.  Users appear here once their "
            "trenchchat.user announce has been received."
        )
        no_peers_hint.setWordWrap(True)
        no_peers_hint.setStyleSheet("color: #888; font-size: 10px; margin-top: 2px;")
        layout.addWidget(no_peers_hint)
        self._no_peers_hint = no_peers_hint

        layout.addSpacing(8)

        # --- manual entry fallback ---
        manual_group = QGroupBox("Or enter identity hash manually")
        manual_group.setStyleSheet(
            "QGroupBox { color: #aaa; font-size: 11px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        manual_layout = QVBoxLayout(manual_group)

        self._hash_edit = QLineEdit()
        self._hash_edit.setPlaceholderText("e.g. a3f1c2d4e5b6a7f8…  (hex, 32 chars)")
        self._hash_edit.setFont(QFont("monospace"))
        self._hash_edit.textChanged.connect(self._on_manual_hash_changed)
        manual_layout.addWidget(self._hash_edit)

        layout.addWidget(manual_group)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._populate_list("")

        # Refresh the list every 5 seconds so newly discovered peers appear
        # without the user having to close and reopen the dialog.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start(5_000)

    # --- private helpers ---

    def _populate_list(self, query: str) -> None:
        """Refresh the user list for the given search query.

        Preserves the current selection by identity hash so that a periodic
        refresh does not discard the user's chosen item.
        """
        selected_hex = None
        current = self._user_list.currentItem()
        if current is not None:
            selected_hex = current.data(Qt.ItemDataRole.UserRole)

        self._user_list.clear()
        entries = self._user_directory.search(query)
        for entry in entries:
            peer_hex = entry["identity_hash"]
            display_name = entry["display_name"] or peer_hex[:16] + "\u2026"
            label = f"{display_name}  ({peer_hex[:16]}\u2026)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, peer_hex)
            self._user_list.addItem(item)
            if peer_hex == selected_hex:
                self._user_list.setCurrentItem(item)

        has_entries = self._user_list.count() > 0
        self._no_peers_hint.setVisible(not has_entries and not query)

    def _on_refresh_tick(self) -> None:
        """Periodic refresh: repopulate the list without disturbing the search text."""
        self._populate_list(self._search_edit.text())

    def _update_ok_button(self) -> None:
        """Enable OK if a list item is selected or a valid hash is typed manually."""
        list_selected = self._user_list.currentItem() is not None
        manual_raw = self._hash_edit.text().strip().lower().replace(" ", "")
        manual_valid = len(manual_raw) == 32 and _is_valid_hex(manual_raw)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            list_selected or manual_valid
        )

    def _on_search_changed(self, text: str) -> None:
        self._populate_list(text)
        self._update_ok_button()

    def _on_list_selection_changed(self, current, _previous) -> None:
        if current is not None:
            # Clear manual entry when a list item is selected
            self._hash_edit.blockSignals(True)
            self._hash_edit.clear()
            self._hash_edit.blockSignals(False)
        self._update_ok_button()

    def _on_manual_hash_changed(self, text: str) -> None:
        if text.strip():
            # Clear list selection when manual entry is used
            self._user_list.clearSelection()
        self._update_ok_button()

    def _on_list_double_clicked(self, item: QListWidgetItem) -> None:
        """Accept immediately on double-click."""
        self._on_accept()

    def _on_accept(self) -> None:
        list_item = self._user_list.currentItem()
        if list_item is not None:
            self._invitee_hash = list_item.data(Qt.ItemDataRole.UserRole)
            self.accept()
            return

        raw = self._hash_edit.text().strip().lower().replace(" ", "")
        if len(raw) != 32:
            QMessageBox.warning(
                self, "Invalid hash",
                "Identity hashes are 32 hex characters (16 bytes).\n"
                f"You entered {len(raw)} characters."
            )
            return
        if not _is_valid_hex(raw):
            QMessageBox.warning(self, "Invalid hash", "That doesn't look like a valid hex string.")
            return
        self._invitee_hash = raw
        self.accept()

    @property
    def invitee_hash(self) -> str | None:
        """The selected or entered identity hash hex, or None if cancelled."""
        return self._invitee_hash


def _is_valid_hex(value: str) -> bool:
    """Return True if value is a non-empty valid hex string."""
    try:
        bytes.fromhex(value)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# MembersDialog — view members, promote/demote admins, remove members
# ---------------------------------------------------------------------------

class MembersDialog(QDialog):
    ROLE_DATA = Qt.ItemDataRole.UserRole + 1

    def __init__(self, channel_hash_hex: str, channel_name: str,
                 storage: Storage, own_hash_hex: str,
                 is_local_admin: bool, parent=None):
        super().__init__(parent)
        self._channel_hash = channel_hash_hex
        self._storage = storage
        self._own_hex = own_hash_hex

        self._can_kick = storage.has_permission(channel_hash_hex, own_hash_hex, KICK)
        self._can_manage_roles = storage.has_permission(channel_hash_hex, own_hash_hex, MANAGE_ROLES)

        self.setWindowTitle(f"Members — #{channel_name}")
        self.setMinimumWidth(480)
        self.setMinimumHeight(320)

        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background: #1e1e1e; border: 1px solid #444; }"
            "QListWidget::item { padding: 6px 8px; color: #ccc; }"
            "QListWidget::item:selected { background: #2a4a7a; }"
        )
        layout.addWidget(self._list)

        if self._can_kick or self._can_manage_roles:
            btn_row = QHBoxLayout()
            if self._can_kick:
                self._remove_btn = QPushButton("Remove member")
                self._remove_btn.setEnabled(False)
                self._remove_btn.clicked.connect(self._on_remove)
                btn_row.addWidget(self._remove_btn)

            if self._can_manage_roles:
                self._toggle_admin_btn = QPushButton("Toggle admin")
                self._toggle_admin_btn.setEnabled(False)
                self._toggle_admin_btn.clicked.connect(self._on_toggle_admin)
                btn_row.addWidget(self._toggle_admin_btn)

            btn_row.addStretch()
            layout.addLayout(btn_row)

            self._list.currentItemChanged.connect(self._on_selection_changed)

        close_btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

        self.members_to_remove: list[bytes] = []
        self.admins_to_add: list[bytes] = []
        self.admins_to_remove: list[bytes] = []

        self._populate()

    def _populate(self):
        self._list.clear()
        for row in self._storage.get_members(self._channel_hash):
            label = row["display_name"] or row["identity_hash"][:16] + "…"
            role = row["role"]
            role_tag = f"  [{role}]" if role in (ROLE_OWNER, ROLE_ADMIN) else ""
            own_tag = "  (you)" if row["identity_hash"] == self._own_hex else ""
            item = QListWidgetItem(f"{label}{role_tag}{own_tag}")
            item.setData(Qt.ItemDataRole.UserRole, row["identity_hash"])
            item.setData(self.ROLE_DATA, role)
            self._list.addItem(item)

    def _on_selection_changed(self, current, _previous):
        has_sel = current is not None
        is_self = has_sel and current.data(Qt.ItemDataRole.UserRole) == self._own_hex
        is_owner = has_sel and current.data(self.ROLE_DATA) == ROLE_OWNER
        actionable = has_sel and not is_self and not is_owner
        if hasattr(self, "_remove_btn"):
            self._remove_btn.setEnabled(actionable)
        if hasattr(self, "_toggle_admin_btn"):
            self._toggle_admin_btn.setEnabled(actionable)

    def _on_remove(self):
        item = self._list.currentItem()
        if not item:
            return
        identity_hex = item.data(Qt.ItemDataRole.UserRole)
        confirm = QMessageBox.question(
            self, "Remove member",
            f"Remove {item.text().split('  ')[0]} from this channel?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.members_to_remove.append(bytes.fromhex(identity_hex))
            self._list.takeItem(self._list.row(item))

    def _on_toggle_admin(self):
        item = self._list.currentItem()
        if not item:
            return
        identity_hex = item.data(Qt.ItemDataRole.UserRole)
        role = item.data(self.ROLE_DATA)
        identity_bytes = bytes.fromhex(identity_hex)
        if role == ROLE_ADMIN:
            self.admins_to_remove.append(identity_bytes)
        elif role == ROLE_MEMBER:
            self.admins_to_add.append(identity_bytes)
        self._populate()


# ---------------------------------------------------------------------------
# ChannelPermissionsDialog — owner / MANAGE_CHANNEL admin edits role permissions
# ---------------------------------------------------------------------------

class ChannelPermissionsDialog(QDialog):
    """Edit per-role permissions and channel flags for a channel.

    The caller is responsible for checking that the current user holds the
    MANAGE_CHANNEL permission before opening this dialog.  On acceptance,
    read back the updated permissions dict via the ``permissions`` property
    and persist it through the appropriate core manager.
    """

    def __init__(self, channel_name: str, current_permissions: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Channel permissions — #{channel_name}")
        self.setMinimumWidth(460)

        self._perms = dict(current_permissions)

        layout = QVBoxLayout(self)

        # --- Channel flags ---
        flags_group = QGroupBox("Channel flags")
        flags_layout = QVBoxLayout(flags_group)

        self._open_join_cb = QCheckBox("Open join (anyone can join without an invite)")
        self._open_join_cb.setChecked(bool(self._perms.get(FLAG_OPEN_JOIN, False)))
        flags_layout.addWidget(self._open_join_cb)

        self._discoverable_cb = QCheckBox("Discoverable (visible in the Join Channel list)")
        self._discoverable_cb.setChecked(bool(self._perms.get(FLAG_DISCOVERABLE, True)))
        flags_layout.addWidget(self._discoverable_cb)

        layout.addWidget(flags_group)

        # --- Per-role permission checkboxes ---
        # Owner always has every permission — display as read-only info row.
        owner_group = QGroupBox(f"Owner  (always has all permissions)")
        owner_layout = QVBoxLayout(owner_group)
        for perm in ALL_PERMISSIONS:
            cb = QCheckBox(_PERMISSION_LABELS[perm])
            cb.setChecked(True)
            cb.setEnabled(False)
            owner_layout.addWidget(cb)
        layout.addWidget(owner_group)

        self._role_checks: dict[str, dict[str, QCheckBox]] = {}
        for role in (ROLE_ADMIN, ROLE_MEMBER):
            group = QGroupBox(role.capitalize())
            group_layout = QVBoxLayout(group)
            role_checks: dict[str, QCheckBox] = {}
            current_role_perms: list[str] = self._perms.get(role, [])
            for perm in ALL_PERMISSIONS:
                cb = QCheckBox(_PERMISSION_LABELS[perm])
                cb.setChecked(perm in current_role_perms)
                group_layout.addWidget(cb)
                role_checks[perm] = cb
            self._role_checks[role] = role_checks
            layout.addWidget(group)

        hint = QLabel(
            "Changes take effect immediately for this device. "
            "Publish a new member list to propagate them to other members."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px; margin-top: 4px;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def permissions(self) -> dict:
        """Return the updated permissions dict reflecting the current checkbox state."""
        result = dict(self._perms)
        result[FLAG_OPEN_JOIN] = self._open_join_cb.isChecked()
        result[FLAG_DISCOVERABLE] = self._discoverable_cb.isChecked()
        for role, checks in self._role_checks.items():
            result[role] = [perm for perm, cb in checks.items() if cb.isChecked()]
        return result


# ---------------------------------------------------------------------------
# IncomingInviteDialog — shown to the invitee when they receive an invite
# ---------------------------------------------------------------------------

class IncomingInviteDialog(QDialog):
    def __init__(self, channel_name: str, admin_hash_hex: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Channel Invite")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        icon = QLabel("📨")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 32px; margin: 8px;")
        layout.addWidget(icon)

        msg = QLabel(f"You have been invited to join <b>#{channel_name}</b>.")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        layout.addWidget(msg)

        from_label = QLabel(
            f"<span style='color:#888;font-size:10px'>Invited by: {admin_hash_hex[:16]}…</span>"
        )
        from_label.setTextFormat(Qt.TextFormat.RichText)
        from_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(from_label)

        layout.addSpacing(12)

        btn_row = QHBoxLayout()
        accept_btn = QPushButton("Accept")
        accept_btn.setStyleSheet("background: #2d7a2d;")
        accept_btn.clicked.connect(self.accept)
        btn_row.addWidget(accept_btn)

        decline_btn = QPushButton("Decline")
        decline_btn.setStyleSheet("background: #7a2d2d;")
        decline_btn.clicked.connect(self.reject)
        btn_row.addWidget(decline_btn)

        layout.addLayout(btn_row)
