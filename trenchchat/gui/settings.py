"""
Settings dialog: identity, propagation node configuration, channel filter.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QSpinBox,
    QComboBox, QListWidget, QListWidgetItem, QGroupBox,
    QDialogButtonBox, QWidget, QTabWidget,
)
from PyQt6.QtCore import Qt

from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router


class SettingsDialog(QDialog):
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
        self._config._data["propagation_node"]["channel_filter"]["channel_hashes"] = selected
        self._config.save()

        # Toggle propagation node
        if new_enabled and not self._config.propagation_enabled:
            self._router.enable_propagation()
        elif not new_enabled and self._config.propagation_enabled:
            self._router.disable_propagation()

        self.accept()
