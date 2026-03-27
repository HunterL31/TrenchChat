"""
Settings dialog: identity, propagation node configuration, channel filter,
and security (PIN lock).
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QSpinBox,
    QComboBox, QListWidget, QListWidgetItem, QGroupBox,
    QDialogButtonBox, QWidget, QTabWidget, QMessageBox,
)
from PyQt6.QtCore import Qt

import RNS

from trenchchat.config import Config
from trenchchat.core import lockbox
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router
from trenchchat.gui.pin_dialog import SetPinDialog, ChangePinDialog


class SettingsDialog(QDialog):
    """Application settings dialog with Identity, Propagation, and Security tabs."""

    def __init__(self, config: Config, identity: Identity,
                 storage: Storage, router: Router, parent=None):
        super().__init__(parent)
        self._config = config
        self._identity = identity
        self._storage = storage
        self._router = router

        self.setWindowTitle("TrenchChat Settings")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_identity_tab(), "Identity")
        tabs.addTab(self._build_propagation_tab(), "Propagation Node")
        tabs.addTab(self._build_security_tab(), "Security")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # --- identity tab ---

    def _build_identity_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        form.setContentsMargins(12, 12, 12, 12)

        self._display_name_edit = QLineEdit(self._config.display_name)
        form.addRow("Display name:", self._display_name_edit)

        id_label = QLabel(self._identity.hash_hex)
        id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        id_label.setStyleSheet("color: #888; font-family: monospace; font-size: 11px;")
        form.addRow("Identity hash:", id_label)

        outbound_label = QLabel("Outbound propagation node (hex hash):")
        self._outbound_edit = QLineEdit(self._config.outbound_propagation_node or "")
        self._outbound_edit.setPlaceholderText("Leave blank to use direct delivery only")
        form.addRow("Propagation node:", self._outbound_edit)

        return widget

    # --- propagation node tab ---

    def _build_propagation_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)

        # Enable toggle
        self._prop_enabled = QCheckBox("Enable propagation node on this instance")
        self._prop_enabled.setChecked(self._config.propagation_enabled)
        self._prop_enabled.toggled.connect(self._on_prop_toggle)
        layout.addWidget(self._prop_enabled)

        # Node settings group
        self._prop_group = QGroupBox("Node settings")
        self._prop_group.setEnabled(self._config.propagation_enabled)
        form = QFormLayout(self._prop_group)

        self._node_name_edit = QLineEdit(self._config.propagation_node_name)
        self._node_name_edit.setPlaceholderText("e.g. my-relay")
        form.addRow("Node name:", self._node_name_edit)

        self._storage_limit_spin = QSpinBox()
        self._storage_limit_spin.setRange(16, 65536)
        self._storage_limit_spin.setSuffix(" MB")
        self._storage_limit_spin.setValue(self._config.propagation_storage_limit_mb)
        form.addRow("Storage limit:", self._storage_limit_spin)

        self._filter_mode_combo = QComboBox()
        self._filter_mode_combo.addItems(["allowlist", "all"])
        self._filter_mode_combo.setCurrentText(self._config.channel_filter_mode)
        self._filter_mode_combo.currentTextChanged.connect(self._on_filter_mode_change)
        form.addRow("Channel filter:", self._filter_mode_combo)

        layout.addWidget(self._prop_group)

        # Channel checklist
        self._channel_group = QGroupBox("Channels to propagate")
        self._channel_group.setEnabled(
            self._config.propagation_enabled
            and self._config.channel_filter_mode == "allowlist"
        )
        ch_layout = QVBoxLayout(self._channel_group)

        self._channel_list = QListWidget()
        allowed = set(self._config.channel_filter_hashes)
        for row in self._storage.get_all_channels():
            item = QListWidgetItem(f"{row['name']}  ({row['hash'][:12]}…)")
            item.setData(Qt.ItemDataRole.UserRole, row["hash"])
            item.setCheckState(
                Qt.CheckState.Checked if row["hash"] in allowed
                else Qt.CheckState.Unchecked
            )
            self._channel_list.addItem(item)

        ch_layout.addWidget(self._channel_list)
        layout.addWidget(self._channel_group)
        layout.addStretch()
        return widget

    def _on_prop_toggle(self, enabled: bool):
        self._prop_group.setEnabled(enabled)
        self._channel_group.setEnabled(
            enabled and self._filter_mode_combo.currentText() == "allowlist"
        )

    def _on_filter_mode_change(self, mode: str):
        self._channel_group.setEnabled(
            self._prop_enabled.isChecked() and mode == "allowlist"
        )

    # --- security tab ---

    def _build_security_tab(self) -> QWidget:
        """Build the Security tab with PIN lock controls."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        group = QGroupBox("PIN Lock")
        grp_layout = QVBoxLayout(group)
        grp_layout.setSpacing(8)

        if lockbox.is_locked():
            status_text = (
                "Your identity and message database are protected by a PIN."
            )
        else:
            status_text = (
                "No PIN is set. Your identity file and message database are "
                "stored unencrypted."
            )
        self._pin_status_label = QLabel(status_text)
        self._pin_status_label.setWordWrap(True)
        grp_layout.addWidget(self._pin_status_label)

        btn_row = QHBoxLayout()

        if lockbox.is_locked():
            change_btn = QPushButton("Change PIN…")
            change_btn.clicked.connect(self._on_change_pin)
            btn_row.addWidget(change_btn)

            remove_btn = QPushButton("Remove PIN…")
            remove_btn.clicked.connect(self._on_remove_pin)
            btn_row.addWidget(remove_btn)
        else:
            set_btn = QPushButton("Set PIN…")
            set_btn.clicked.connect(self._on_set_pin)
            btn_row.addWidget(set_btn)

        btn_row.addStretch()
        grp_layout.addLayout(btn_row)

        warning = QLabel(
            "<i>Note: changing or removing the PIN re-encrypts or decrypts "
            "your data files immediately. TrenchChat must be restarted for "
            "the new lock state to take effect on startup.</i>"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #888; font-size: 11px;")
        grp_layout.addWidget(warning)

        layout.addWidget(group)
        layout.addStretch()
        return widget

    def _reopen_storage(self, encryption_key: bytes | None) -> None:
        """Re-initialise the storage connection after a PIN change.

        The path is preserved from the current (closed) instance.
        """
        db_path = Path(self._storage._path)
        self._storage.__init__(db_path=db_path, encryption_key=encryption_key)

    def _on_set_pin(self):
        """Set a new PIN, encrypting identity and database."""
        dlg = SetPinDialog(self)
        if dlg.exec() != SetPinDialog.DialogCode.Accepted or dlg.pin is None:
            return

        try:
            new_key = lockbox.create_lock(dlg.pin)
            # Re-encrypt identity file with the new key.
            self._identity.reencrypt(old_key=None, new_key=new_key)
            # Close the storage connection, encrypt the DB, then reopen.
            self._storage.close()
            self._storage.encrypt_database(new_key)
            self._reopen_storage(new_key)
        except Exception as exc:
            RNS.log(f"TrenchChat: failed to set PIN: {exc}", RNS.LOG_ERROR)
            QMessageBox.critical(self, "Error", f"Failed to set PIN:\n{exc}")
            return

        self._pin_status_label.setText(
            "Your identity and message database are protected by a PIN."
        )
        QMessageBox.information(
            self, "PIN Set",
            "PIN lock enabled. You will need your PIN the next time you start TrenchChat."
        )
        RNS.log("TrenchChat: PIN lock enabled via settings", RNS.LOG_NOTICE)

    def _on_change_pin(self):
        """Change the existing PIN, re-keying the database."""
        dlg = ChangePinDialog(self)
        if dlg.exec() != ChangePinDialog.DialogCode.Accepted:
            return
        if dlg.new_pin is None:
            # User decided to remove — delegate to remove logic.
            self._remove_pin_with_key(dlg.current_raw_key)
            return

        try:
            # Remove old lock metadata and create fresh salt + verify for new PIN.
            lockbox.remove_lock()
            new_key = lockbox.create_lock(dlg.new_pin)

            self._identity.reencrypt(
                old_key=dlg.current_raw_key,
                new_key=new_key,
            )
            self._storage.close()
            self._storage.rekey_database(dlg.current_raw_key, new_key)
            self._reopen_storage(new_key)
        except Exception as exc:
            RNS.log(f"TrenchChat: failed to change PIN: {exc}", RNS.LOG_ERROR)
            QMessageBox.critical(self, "Error", f"Failed to change PIN:\n{exc}")
            return

        QMessageBox.information(self, "PIN Changed", "Your PIN has been updated.")
        RNS.log("TrenchChat: PIN changed via settings", RNS.LOG_NOTICE)

    def _on_remove_pin(self):
        """Remove the PIN, decrypting identity and database."""
        dlg = ChangePinDialog(self)
        dlg.setWindowTitle("Remove PIN")
        if dlg.exec() != ChangePinDialog.DialogCode.Accepted:
            return
        self._remove_pin_with_key(dlg.current_raw_key)

    def _remove_pin_with_key(self, current_key: bytes | None) -> None:
        """Decrypt data and remove the lock using a pre-verified key."""
        if current_key is None:
            return
        try:
            self._identity.reencrypt(old_key=current_key, new_key=None)
            self._storage.close()
            self._storage.decrypt_database(current_key)
            lockbox.remove_lock()
            self._reopen_storage(None)
        except Exception as exc:
            RNS.log(f"TrenchChat: failed to remove PIN: {exc}", RNS.LOG_ERROR)
            QMessageBox.critical(self, "Error", f"Failed to remove PIN:\n{exc}")
            return

        self._pin_status_label.setText(
            "No PIN is set. Your identity file and message database are "
            "stored unencrypted."
        )
        QMessageBox.information(
            self, "PIN Removed",
            "PIN lock removed. Your data is no longer encrypted at rest."
        )
        RNS.log("TrenchChat: PIN lock removed via settings", RNS.LOG_NOTICE)

    # --- accept ---

    def _on_accept(self):
        # Identity / outbound
        self._config.display_name = self._display_name_edit.text().strip() or "Anonymous"
        outbound = self._outbound_edit.text().strip()
        if outbound != (self._config.outbound_propagation_node or ""):
            self._router.set_outbound_propagation_node(outbound or None)

        # Propagation node
        new_enabled = self._prop_enabled.isChecked()
        self._config.propagation_node_name = self._node_name_edit.text().strip()
        self._config.propagation_storage_limit_mb = self._storage_limit_spin.value()
        self._config.channel_filter_mode = self._filter_mode_combo.currentText()

        # Channel filter hashes
        selected = []
        for i in range(self._channel_list.count()):
            item = self._channel_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(item.data(Qt.ItemDataRole.UserRole))
        self._config.set_channel_filter_hashes(selected)

        # Toggle propagation node
        if new_enabled and not self._config.propagation_enabled:
            self._router.enable_propagation()
        elif not new_enabled and self._config.propagation_enabled:
            self._router.disable_propagation()

        self.accept()
