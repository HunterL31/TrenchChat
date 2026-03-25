"""
Interfaces tab widget for viewing and editing Reticulum interface configurations.

Layout
------
A QWidget containing:
  - A toolbar with Add, Edit, Delete, and Refresh buttons
  - A QTableWidget listing all interfaces from the Reticulum config file with
    columns: Name, Type, Enabled, Status, RX, TX
  - Live stats (status, rx/tx bytes) are merged from rns.get_interface_stats()

Editing
-------
Add and Edit open an InterfaceDialog that provides type-specific form fields.
Changes are written to the Reticulum config file via ConfigObj and the user is
prompted to restart for them to take effect.

Supported types for create/edit
--------------------------------
  AutoInterface, TCPClientInterface, TCPServerInterface, UDPInterface,
  SerialInterface, RNodeInterface

Interfaces of other types already in the config are displayed read-only.
"""

import os

import RNS
from configobj import ConfigObj

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QDialog, QScrollArea, QSizePolicy, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STATS_REFRESH_MS = 5_000

# Interface types that can be created or edited through the GUI.
EDITABLE_TYPES = [
    "AutoInterface",
    "TCPClientInterface",
    "TCPServerInterface",
    "UDPInterface",
    "SerialInterface",
    "RNodeInterface",
]

# Table column indices
_COL_NAME    = 0
_COL_TYPE    = 1
_COL_ENABLED = 2
_COL_STATUS  = 3
_COL_RX      = 4
_COL_TX      = 5

# Common fields shown for every interface type.
# Each entry: (config_key, label, field_type, default)
# field_type is one of: str, int, float, bool, or a list of strings (combo).
_COMMON_FIELDS: list[tuple[str, str, object, object]] = [
    ("interface_mode", "Interface mode",
     ["full", "access_point", "pointtopoint", "roaming", "boundary", "gateway"],
     "full"),
    ("networkname",    "Network name",    str,  ""),
    ("passphrase",     "Passphrase",      str,  ""),
    ("bitrate",        "Bitrate (bps)",   int,  0),
    ("announce_cap",   "Announce cap (%)", float, 2.0),
]

# Per-type specific fields.
_TYPE_FIELDS: dict[str, list[tuple[str, str, object, object]]] = {
    "AutoInterface": [
        ("group_id",         "Group ID",         str,  "reticulum"),
        ("discovery_scope",  "Discovery scope",
         ["link", "admin", "site", "organisation", "global"], "link"),
        ("discovery_port",   "Discovery port",   int,  29716),
        ("data_port",        "Data port",        int,  42671),
        ("devices",          "Allowed devices (comma-separated)", str, ""),
        ("ignored_devices",  "Ignored devices (comma-separated)", str, ""),
    ],
    "TCPClientInterface": [
        ("target_host",         "Target host",           str,  ""),
        ("target_port",         "Target port",           int,  4965),
        ("kiss_framing",        "KISS framing",          bool, False),
        ("i2p_tunneled",        "I2P tunneled",          bool, False),
        ("connect_timeout",     "Connect timeout (s)",   int,  5),
        ("max_reconnect_tries", "Max reconnect tries (0 = unlimited)", int, 0),
    ],
    "TCPServerInterface": [
        ("listen_ip",    "Listen IP",    str,  "0.0.0.0"),
        ("listen_port",  "Listen port",  int,  4965),
        ("i2p_tunneled", "I2P tunneled", bool, False),
        ("prefer_ipv6",  "Prefer IPv6",  bool, False),
    ],
    "UDPInterface": [
        ("listen_ip",    "Listen IP",    str, "0.0.0.0"),
        ("listen_port",  "Listen port",  int, 4242),
        ("forward_ip",   "Forward IP",   str, "255.255.255.255"),
        ("forward_port", "Forward port", int, 4242),
    ],
    "SerialInterface": [
        ("port",     "Serial port",  str,  ""),
        ("speed",    "Baud rate",    int,  9600),
        ("databits", "Data bits",    int,  8),
        ("parity",   "Parity",       ["N", "E", "O"], "N"),
        ("stopbits", "Stop bits",    int,  1),
    ],
    "RNodeInterface": [
        ("port",           "Serial port",        str,   ""),
        ("frequency",      "Frequency (Hz)",     int,   868000000),
        ("bandwidth",      "Bandwidth (Hz)",     int,   125000),
        ("txpower",        "TX power (dBm)",     int,   14),
        ("spreadingfactor","Spreading factor",   int,   8),
        ("codingrate",     "Coding rate",        int,   5),
        ("flow_control",   "Flow control",       bool,  False),
        ("id_interval",    "ID interval (s)",    int,   0),
        ("id_callsign",    "ID callsign",        str,   ""),
        ("airtime_limit_short", "Airtime limit short (%)", float, 0.0),
        ("airtime_limit_long",  "Airtime limit long (%)",  float, 0.0),
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    """Format a byte count as a human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def load_interfaces_config(config_path: str) -> dict[str, dict]:
    """Read the [interfaces] section from the Reticulum config file.

    Returns a dict mapping interface name to its config dict (including 'type').
    Returns an empty dict if the file does not exist or has no [interfaces] section.
    """
    if not os.path.isfile(config_path):
        return {}
    try:
        cfg = ConfigObj(config_path)
    except Exception:
        return {}
    interfaces_section = cfg.get("interfaces", {})
    result = {}
    for name, section in interfaces_section.items():
        if isinstance(section, dict):
            result[name] = dict(section)
    return result


def build_interface_config_dict(
    name: str,
    iface_type: str,
    enabled: bool,
    type_values: dict[str, str],
    common_values: dict[str, str],
) -> dict[str, str]:
    """Assemble a flat config dict for a single interface section.

    All values are stored as strings (ConfigObj INI format).
    """
    cfg: dict[str, str] = {"type": iface_type, "enabled": "Yes" if enabled else "No"}
    for key, value in type_values.items():
        if value != "":
            cfg[key] = str(value)
    for key, value in common_values.items():
        if value != "":
            cfg[key] = str(value)
    return cfg


# ---------------------------------------------------------------------------
# InterfaceDialog
# ---------------------------------------------------------------------------

class InterfaceDialog(QDialog):
    """Dialog for adding or editing a Reticulum interface configuration.

    When editing, the type selector is disabled. On accept the caller should
    call build_config() to retrieve the assembled config dict.
    """

    def __init__(self, existing_name: str = "", existing_cfg: dict | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editing = existing_name != ""
        self._existing_name = existing_name
        self._existing_cfg = existing_cfg or {}

        self.setWindowTitle("Edit Interface" if self._editing else "Add Interface")
        self.setMinimumWidth(480)

        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        inner = QWidget()
        self._form_outer = QVBoxLayout(inner)
        self._form_outer.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # --- Name and type ---
        header_form = QFormLayout()
        header_form.setContentsMargins(12, 12, 12, 4)

        self._name_edit = QLineEdit(existing_name)
        self._name_edit.setPlaceholderText("e.g. My TCP Hub")
        if self._editing:
            self._name_edit.setEnabled(False)
        header_form.addRow("Interface name:", self._name_edit)

        self._type_combo = QComboBox()
        self._type_combo.addItems(EDITABLE_TYPES)
        if self._editing:
            iface_type = self._existing_cfg.get("type", EDITABLE_TYPES[0])
            idx = self._type_combo.findText(iface_type)
            self._type_combo.setCurrentIndex(max(idx, 0))
            self._type_combo.setEnabled(False)
        self._type_combo.currentTextChanged.connect(self._rebuild_type_fields)
        header_form.addRow("Type:", self._type_combo)

        self._enabled_check = QCheckBox("Enabled")
        enabled_str = self._existing_cfg.get("enabled",
                      self._existing_cfg.get("interface_enabled", "Yes"))
        self._enabled_check.setChecked(enabled_str.lower() in ("yes", "true", "1"))
        header_form.addRow("", self._enabled_check)

        self._form_outer.addLayout(header_form)

        # --- Type-specific fields (rebuilt on type change) ---
        self._type_group = QGroupBox("Type-specific settings")
        self._type_form = QFormLayout(self._type_group)
        self._type_form.setContentsMargins(12, 8, 12, 8)
        self._form_outer.addWidget(self._type_group)

        # --- Common fields ---
        common_group = QGroupBox("Common settings")
        self._common_form = QFormLayout(common_group)
        self._common_form.setContentsMargins(12, 8, 12, 8)
        self._form_outer.addWidget(common_group)

        self._form_outer.addStretch()

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._type_widgets: dict[str, QWidget] = {}
        self._common_widgets: dict[str, QWidget] = {}

        self._build_common_fields()
        self._rebuild_type_fields(self._type_combo.currentText())

    # --- field builders ---

    def _make_field_widget(self, key: str, field_type: object, default: object,
                           existing: dict) -> QWidget:
        """Create the appropriate widget for a field based on its type."""
        raw = existing.get(key, "")

        if isinstance(field_type, list):
            combo = QComboBox()
            combo.addItems(field_type)
            value = raw if raw in field_type else str(default)
            idx = combo.findText(value)
            combo.setCurrentIndex(max(idx, 0))
            return combo

        if field_type is bool:
            check = QCheckBox()
            if raw:
                check.setChecked(raw.lower() in ("yes", "true", "1"))
            else:
                check.setChecked(bool(default))
            return check

        if field_type is int:
            spin = QSpinBox()
            spin.setRange(0, 2_000_000_000)
            try:
                spin.setValue(int(raw) if raw else int(default))
            except (ValueError, TypeError):
                spin.setValue(int(default) if default else 0)
            return spin

        if field_type is float:
            edit = QLineEdit(raw if raw else str(default))
            return edit

        # str
        edit = QLineEdit(raw if raw else str(default) if default else "")
        return edit

    def _rebuild_type_fields(self, iface_type: str) -> None:
        """Clear and repopulate the type-specific fields group."""
        while self._type_form.rowCount():
            self._type_form.removeRow(0)
        self._type_widgets.clear()

        fields = _TYPE_FIELDS.get(iface_type, [])
        for key, label, field_type, default in fields:
            widget = self._make_field_widget(key, field_type, default, self._existing_cfg)
            self._type_widgets[key] = widget
            self._type_form.addRow(label + ":", widget)

    def _build_common_fields(self) -> None:
        """Populate the common fields group."""
        for key, label, field_type, default in _COMMON_FIELDS:
            widget = self._make_field_widget(key, field_type, default, self._existing_cfg)
            self._common_widgets[key] = widget
            self._common_form.addRow(label + ":", widget)

    # --- value extraction ---

    def _widget_value(self, widget: QWidget) -> str:
        """Extract the string value from any field widget."""
        if isinstance(widget, QCheckBox):
            return "Yes" if widget.isChecked() else "No"
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if isinstance(widget, QSpinBox):
            return str(widget.value())
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        return ""

    def _on_accept(self) -> None:
        """Validate required fields before accepting."""
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation", "Interface name is required.")
            return

        iface_type = self._type_combo.currentText()

        # Validate required fields per type
        required_keys: dict[str, list[str]] = {
            "TCPClientInterface": ["target_host"],
            "TCPServerInterface": ["listen_ip", "listen_port"],
            "SerialInterface":    ["port"],
            "RNodeInterface":     ["port"],
            "PipeInterface":      ["command"],
        }
        for key in required_keys.get(iface_type, []):
            widget = self._type_widgets.get(key)
            if widget is None:
                continue
            val = self._widget_value(widget)
            if not val or val == "0":
                label = next(
                    (lbl for k, lbl, *_ in _TYPE_FIELDS.get(iface_type, []) if k == key),
                    key,
                )
                QMessageBox.warning(self, "Validation", f"'{label}' is required.")
                return

        self.accept()

    # --- public API ---

    def interface_name(self) -> str:
        """Return the interface name entered by the user."""
        return self._name_edit.text().strip()

    def build_config(self) -> dict[str, str]:
        """Return a flat config dict suitable for writing to ConfigObj."""
        iface_type = self._type_combo.currentText()
        enabled = "Yes" if self._enabled_check.isChecked() else "No"

        cfg: dict[str, str] = {"type": iface_type, "enabled": enabled}

        for key, widget in self._type_widgets.items():
            val = self._widget_value(widget)
            if val and val not in ("0", "0.0"):
                cfg[key] = val

        for key, widget in self._common_widgets.items():
            val = self._widget_value(widget)
            # Only write non-default / non-empty common fields
            if val and val not in ("", "0", "0.0"):
                cfg[key] = val

        return cfg


# ---------------------------------------------------------------------------
# InterfacesWidget
# ---------------------------------------------------------------------------

class InterfacesWidget(QWidget):
    """Tab widget that lists Reticulum interfaces and allows editing them.

    Reads interface configuration from the Reticulum config file (via ConfigObj)
    and merges live stats from rns.get_interface_stats() for status display.
    Changes are written back to the config file; a restart prompt is shown.
    """

    def __init__(self, rns: "RNS.Reticulum", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rns = rns
        self._config_path = RNS.Reticulum.configpath
        self._stats_timer: QTimer | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Toolbar ---
        toolbar = QWidget()
        toolbar.setStyleSheet("background: #1e1e1e; border-bottom: 1px solid #333;")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 6, 8, 6)
        tb_layout.setSpacing(6)

        self._add_btn = QPushButton("+ Add")
        self._edit_btn = QPushButton("✎ Edit")
        self._delete_btn = QPushButton("✕ Delete")
        self._refresh_btn = QPushButton("↻ Refresh")

        _btn_style = (
            "QPushButton { background: #2a2a2a; color: #ccc; border: 1px solid #444;"
            " border-radius: 3px; padding: 3px 10px; font-size: 12px; }"
            "QPushButton:hover { background: #3a3a3a; }"
            "QPushButton:disabled { color: #555; }"
        )
        for btn in (self._add_btn, self._edit_btn, self._delete_btn, self._refresh_btn):
            btn.setStyleSheet(_btn_style)

        self._add_btn.clicked.connect(self._on_add)
        self._edit_btn.clicked.connect(self._on_edit)
        self._delete_btn.clicked.connect(self._on_delete)
        self._refresh_btn.clicked.connect(self._on_refresh)

        tb_layout.addWidget(self._add_btn)
        tb_layout.addWidget(self._edit_btn)
        tb_layout.addWidget(self._delete_btn)
        tb_layout.addStretch()
        tb_layout.addWidget(self._refresh_btn)
        layout.addWidget(toolbar)

        # --- Table ---
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Name", "Type", "Enabled", "Status", "RX", "TX"]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_TYPE, QHeaderView.ResizeMode.ResizeToContents
        )
        for col in (_COL_ENABLED, _COL_STATUS, _COL_RX, _COL_TX):
            self._table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            "QTableWidget { background: #1a1a1a; color: #ddd; gridline-color: #333;"
            " alternate-background-color: #222; border: none; }"
            "QHeaderView::section { background: #252525; color: #aaa;"
            " border: 1px solid #333; padding: 4px; }"
            "QTableWidget::item:selected { background: #2d4a6e; }"
        )
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.doubleClicked.connect(self._on_edit)
        layout.addWidget(self._table, 1)

        # --- Status bar ---
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            "color: #666; font-size: 11px; padding: 4px 8px;"
            " background: #111; border-top: 1px solid #333;"
        )
        layout.addWidget(self._status_label)

        self._update_button_states()
        self.load_interfaces()

    # --- data loading ---

    def load_interfaces(self) -> None:
        """Reload interface config from disk and merge live stats into the table."""
        cfg_interfaces = load_interfaces_config(self._config_path)
        stats_by_name = self._fetch_stats_by_name()

        self._table.setRowCount(0)
        for name, cfg in cfg_interfaces.items():
            row = self._table.rowCount()
            self._table.insertRow(row)

            iface_type = cfg.get("type", "Unknown")
            enabled_str = cfg.get("enabled", cfg.get("interface_enabled", "Yes"))
            enabled = enabled_str.lower() in ("yes", "true", "1")

            stats = stats_by_name.get(name, {})
            online = stats.get("status", None)
            rxb = stats.get("rxb", None)
            txb = stats.get("txb", None)

            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, name)
            self._table.setItem(row, _COL_NAME, name_item)
            self._table.setItem(row, _COL_TYPE, QTableWidgetItem(iface_type))

            enabled_item = QTableWidgetItem("Yes" if enabled else "No")
            enabled_item.setForeground(
                Qt.GlobalColor.green if enabled else Qt.GlobalColor.red
            )
            self._table.setItem(row, _COL_ENABLED, enabled_item)

            if online is None:
                status_text = "—"
                status_color = Qt.GlobalColor.gray
            elif online:
                status_text = "Online"
                status_color = Qt.GlobalColor.green
            else:
                status_text = "Offline"
                status_color = Qt.GlobalColor.red
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(status_color)
            self._table.setItem(row, _COL_STATUS, status_item)

            rx_item = QTableWidgetItem(_fmt_bytes(rxb) if rxb is not None else "—")
            rx_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, _COL_RX, rx_item)

            tx_item = QTableWidgetItem(_fmt_bytes(txb) if txb is not None else "—")
            tx_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, _COL_TX, tx_item)

        count = self._table.rowCount()
        self._status_label.setText(
            f"{count} interface{'s' if count != 1 else ''} configured"
            f"  ·  Config: {self._config_path}"
        )
        self._update_button_states()

    def _fetch_stats_by_name(self) -> dict[str, dict]:
        """Return a dict mapping interface name to its stats dict."""
        try:
            result = self._rns.get_interface_stats()
            interfaces = result.get("interfaces", []) if result else []
            by_name: dict[str, dict] = {}
            for iface in interfaces:
                name = iface.get("name", "")
                # Stats use the full name like "TCPClientInterface[Hub/1.2.3.4:4242]"
                # Config uses the short name. Try both.
                short = iface.get("short_name", name)
                by_name[name] = iface
                by_name[short] = iface
            return by_name
        except Exception as e:
            RNS.log(f"TrenchChat [interfaces]: could not fetch stats: {e}", RNS.LOG_WARNING)
            return {}

    # --- timer control (called by MainWindow when tab is activated/deactivated) ---

    def start_refresh_timer(self) -> None:
        """Start the periodic stats refresh timer."""
        if self._stats_timer is None:
            self._stats_timer = QTimer(self)
            self._stats_timer.timeout.connect(self._on_stats_refresh)
        self._stats_timer.start(_STATS_REFRESH_MS)

    def stop_refresh_timer(self) -> None:
        """Stop the periodic stats refresh timer."""
        if self._stats_timer is not None:
            self._stats_timer.stop()

    # --- button handlers ---

    def _on_refresh(self) -> None:
        """Manual refresh."""
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("…")
        self.load_interfaces()
        QTimer.singleShot(600, lambda: (
            self._refresh_btn.setEnabled(True),
            self._refresh_btn.setText("↻ Refresh"),
        ))

    def _on_stats_refresh(self) -> None:
        """Periodic stats-only refresh (updates status/rx/tx without rebuilding rows)."""
        stats_by_name = self._fetch_stats_by_name()
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, _COL_NAME)
            if name_item is None:
                continue
            name = name_item.data(Qt.ItemDataRole.UserRole)
            stats = stats_by_name.get(name, {})
            online = stats.get("status", None)
            rxb = stats.get("rxb", None)
            txb = stats.get("txb", None)

            if online is None:
                status_text, status_color = "—", Qt.GlobalColor.gray
            elif online:
                status_text, status_color = "Online", Qt.GlobalColor.green
            else:
                status_text, status_color = "Offline", Qt.GlobalColor.red

            status_item = self._table.item(row, _COL_STATUS)
            if status_item:
                status_item.setText(status_text)
                status_item.setForeground(status_color)

            rx_item = self._table.item(row, _COL_RX)
            if rx_item:
                rx_item.setText(_fmt_bytes(rxb) if rxb is not None else "—")

            tx_item = self._table.item(row, _COL_TX)
            if tx_item:
                tx_item.setText(_fmt_bytes(txb) if txb is not None else "—")

    def _on_add(self) -> None:
        """Open the add-interface dialog."""
        dlg = InterfaceDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.interface_name()
        cfg = dlg.build_config()
        self._write_interface(name, cfg, is_new=True)

    def _on_edit(self) -> None:
        """Open the edit-interface dialog for the selected row."""
        name = self._selected_name()
        if not name:
            return

        iface_type = self._table.item(
            self._table.currentRow(), _COL_TYPE
        ).text()
        if iface_type not in EDITABLE_TYPES:
            QMessageBox.information(
                self, "Read-only",
                f"Interfaces of type '{iface_type}' cannot be edited through this GUI.\n"
                "Edit the Reticulum config file directly."
            )
            return

        cfg_interfaces = load_interfaces_config(self._config_path)
        existing_cfg = cfg_interfaces.get(name, {})
        dlg = InterfaceDialog(existing_name=name, existing_cfg=existing_cfg, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        cfg = dlg.build_config()
        self._write_interface(name, cfg, is_new=False)

    def _on_delete(self) -> None:
        """Delete the selected interface from the config file."""
        name = self._selected_name()
        if not name:
            return
        reply = QMessageBox.question(
            self, "Delete Interface",
            f"Delete interface '{name}' from the Reticulum config?\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            cfg = ConfigObj(self._config_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read config file:\n{e}")
            return

        interfaces = cfg.get("interfaces", {})
        if name in interfaces:
            del interfaces[name]
            cfg["interfaces"] = interfaces
            cfg.write()
            RNS.log(f"TrenchChat [interfaces]: deleted interface '{name}'", RNS.LOG_NOTICE)
            self.load_interfaces()
            self._show_restart_prompt()

    def _on_selection_changed(self) -> None:
        self._update_button_states()

    # --- helpers ---

    def _selected_name(self) -> str | None:
        """Return the interface name for the currently selected row, or None."""
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, _COL_NAME)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_button_states(self) -> None:
        """Enable/disable Edit and Delete based on whether a row is selected."""
        has_selection = self._table.currentRow() >= 0
        self._edit_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)

    def _write_interface(self, name: str, cfg_dict: dict[str, str],
                         is_new: bool) -> None:
        """Write a single interface section to the config file and refresh."""
        try:
            file_cfg = ConfigObj(self._config_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read config file:\n{e}")
            return

        if "interfaces" not in file_cfg:
            file_cfg["interfaces"] = {}

        if is_new and name in file_cfg["interfaces"]:
            QMessageBox.warning(
                self, "Duplicate Name",
                f"An interface named '{name}' already exists."
            )
            return

        file_cfg["interfaces"][name] = cfg_dict
        try:
            file_cfg.write()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not write config file:\n{e}")
            return

        action = "Added" if is_new else "Updated"
        RNS.log(
            f"TrenchChat [interfaces]: {action.lower()} interface '{name}'",
            RNS.LOG_NOTICE,
        )
        self.load_interfaces()
        self._show_restart_prompt()

    def _show_restart_prompt(self) -> None:
        """Inform the user that a restart is required for changes to take effect."""
        QMessageBox.information(
            self,
            "Restart Required",
            "Interface configuration has been saved.\n\n"
            "Changes will take effect after restarting TrenchChat "
            "(or the Reticulum shared instance if you are using one).",
        )
