"""
Invite-related dialogs:
  - InviteDialog       : admin sends an invite (enters invitee hash)
  - MembersDialog      : view/remove members for a channel
  - IncomingInviteDialog: invitee accepts or declines an invite
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem,
    QDialogButtonBox, QMessageBox, QWidget,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from trenchchat.core.permissions import (
    INVITE, KICK, MANAGE_ROLES, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
)
from trenchchat.core.storage import Storage


# ---------------------------------------------------------------------------
# InviteDialog — admin enters an invitee identity hash and sends an invite
# ---------------------------------------------------------------------------

class InviteDialog(QDialog):
    def __init__(self, channel_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Invite to #{channel_name}")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        info = QLabel(
            "Enter the Reticulum identity hash of the person you want to invite.\n"
            "They must share their identity hash with you out-of-band."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; font-size: 11px; margin-bottom: 8px;")
        layout.addWidget(info)

        form = QFormLayout()
        self._hash_edit = QLineEdit()
        self._hash_edit.setPlaceholderText("e.g. a3f1c2d4e5b6a7f8…  (hex, 32 chars)")
        self._hash_edit.setFont(QFont("monospace"))
        form.addRow("Identity hash:", self._hash_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._invitee_hash: str | None = None

    def _on_accept(self):
        raw = self._hash_edit.text().strip().lower().replace(" ", "")
        if len(raw) != 32:
            QMessageBox.warning(
                self, "Invalid hash",
                "Identity hashes are 32 hex characters (16 bytes).\n"
                f"You entered {len(raw)} characters."
            )
            return
        try:
            bytes.fromhex(raw)
        except ValueError:
            QMessageBox.warning(self, "Invalid hash", "That doesn't look like a valid hex string.")
            return
        self._invitee_hash = raw
        self.accept()

    @property
    def invitee_hash(self) -> str | None:
        return self._invitee_hash


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
