"""
Settings dialog: identity, propagation node configuration, channel filter,
and security (PIN lock).
"""

import io
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QSpinBox,
    QComboBox, QListWidget, QListWidgetItem, QGroupBox,
    QDialogButtonBox, QWidget, QTabWidget, QMessageBox,
    QFileDialog, QScrollArea, QSizePolicy,
)
from PyQt6.QtCore import Qt, QPoint, QRect, QSize
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath, QColor, QImage, QPen

import RNS

from trenchchat.config import Config
from trenchchat.core import lockbox
from trenchchat.core.avatar import compress_avatar, MAX_AVATAR_BYTES
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router
from trenchchat.gui.pin_dialog import SetPinDialog, ChangePinDialog

_PREVIEW_SIZE = 80   # displayed avatar size in the settings dialog (px)
_CROP_PREVIEW_SIZE = 280   # crop dialog canvas size (px)


def _make_circular_pixmap(pixmap: QPixmap, size: int) -> QPixmap:
    """Return a new size×size pixmap with the source rendered as a circle."""
    scaled = pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    # Center-crop if the scaled result isn't square
    if scaled.width() > size or scaled.height() > size:
        x = (scaled.width() - size) // 2
        y = (scaled.height() - size) // 2
        scaled = scaled.copy(x, y, size, size)

    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)

    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, scaled)
    painter.end()
    return result


class AvatarCropDialog(QDialog):
    """Displays a loaded image with a draggable, zoomable circular crop overlay.

    Drag to pan; scroll wheel to zoom in/out.  On accept, cropped_bytes
    contains the final 48x48 JPEG bytes.
    """

    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop Profile Picture")
        self.setFixedSize(_CROP_PREVIEW_SIZE + 40, _CROP_PREVIEW_SIZE + 140)
        self.cropped_bytes: bytes | None = None

        self._source = QPixmap(image_path)
        if self._source.isNull():
            QMessageBox.critical(self, "Error", "Could not load the selected image.")
            return

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        self._canvas = _CropCanvas(self._source, _CROP_PREVIEW_SIZE, self)
        layout.addWidget(self._canvas, alignment=Qt.AlignmentFlag.AlignCenter)

        hint = QLabel("Drag to pan  •  Scroll to zoom")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        """Render the current crop region into a 48×48 JPEG."""
        crop_pixmap = self._canvas.get_crop_pixmap()
        if crop_pixmap is None or crop_pixmap.isNull():
            self.reject()
            return

        qimage = crop_pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
        ptr = qimage.bits()
        ptr.setsize(qimage.sizeInBytes())
        from PIL import Image as _PILImage
        pil_img = _PILImage.frombytes(
            "RGB", (qimage.width(), qimage.height()), bytes(ptr)
        )
        pil_img = pil_img.resize((48, 48), _PILImage.LANCZOS)
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=70, optimize=True)
        result = buf.getvalue()

        if len(result) > MAX_AVATAR_BYTES:
            QMessageBox.warning(
                self, "Image Too Large",
                f"The cropped image is {len(result)} bytes after compression "
                f"(max {MAX_AVATAR_BYTES}).\nTry a simpler image."
            )
            return

        self.cropped_bytes = result
        self.accept()


class _CropCanvas(QWidget):
    """Canvas that shows the source image with a fixed circular crop window.

    Pan:  drag with left mouse button.
    Zoom: scroll wheel (the image is scaled around the canvas centre).

    The circular crop window is always the full canvas diameter.  The source
    image is scaled and translated so that the region inside the circle is
    what will be cropped.
    """

    _ZOOM_STEP = 0.1
    _ZOOM_MIN = 0.5
    _ZOOM_MAX = 8.0

    def __init__(self, source_pixmap: QPixmap, canvas_size: int, parent=None):
        super().__init__(parent)
        self.setFixedSize(canvas_size, canvas_size)
        self.setFocusPolicy(Qt.FocusPolicy.WheelFocus)
        self._source = source_pixmap
        self._canvas_size = canvas_size

        # Start zoom so the shorter side fills the canvas exactly
        src_w = source_pixmap.width()
        src_h = source_pixmap.height()
        shorter = min(src_w, src_h)
        self._zoom = canvas_size / shorter if shorter > 0 else 1.0
        self._zoom = max(self._ZOOM_MIN, min(self._ZOOM_MAX, self._zoom))

        # Image origin in canvas coords (top-left of the scaled image)
        self._img_x: float = 0.0
        self._img_y: float = 0.0
        self._center_image()

        self._drag_start: QPoint | None = None
        self._img_start: tuple[float, float] | None = None

    # --- public ---

    def get_crop_pixmap(self) -> QPixmap:
        """Render the current view into a canvas_size × canvas_size pixmap."""
        result = QPixmap(self._canvas_size, self._canvas_size)
        result.fill(Qt.GlobalColor.black)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scaled_w = int(self._source.width() * self._zoom)
        scaled_h = int(self._source.height() * self._zoom)
        scaled = self._source.scaled(
            scaled_w, scaled_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(int(self._img_x), int(self._img_y), scaled)
        painter.end()
        return result

    # --- Qt events ---

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        painter.fillRect(self.rect(), QColor("#111111"))

        # Draw scaled image
        scaled_w = int(self._source.width() * self._zoom)
        scaled_h = int(self._source.height() * self._zoom)
        scaled = self._source.scaled(
            scaled_w, scaled_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(int(self._img_x), int(self._img_y), scaled)

        # Darken outside the crop circle
        cs = self._canvas_size
        overlay = QPainterPath()
        overlay.addRect(0, 0, cs, cs)
        circle = QPainterPath()
        circle.addEllipse(0, 0, cs, cs)
        overlay = overlay.subtracted(circle)
        painter.fillPath(overlay, QColor(0, 0, 0, 170))

        # Circle border
        pen = QPen(QColor("#4a9eff"), 2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(1, 1, cs - 2, cs - 2)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
            self._img_start = (self._img_x, self._img_y)

    def mouseMoveEvent(self, event):
        if self._drag_start is not None and self._img_start is not None:
            delta = event.pos() - self._drag_start
            self._img_x = self._img_start[0] + delta.x()
            self._img_y = self._img_start[1] + delta.y()
            self._clamp()
            self.update()

    def mouseReleaseEvent(self, event):
        self._drag_start = None
        self._img_start = None

    def wheelEvent(self, event):
        """Zoom around the canvas centre on scroll."""
        delta = event.angleDelta().y()
        if delta == 0:
            return

        old_zoom = self._zoom
        step = self._ZOOM_STEP * (1 if delta > 0 else -1)
        new_zoom = max(self._ZOOM_MIN, min(self._ZOOM_MAX, old_zoom + step))
        if new_zoom == old_zoom:
            return

        # Keep the canvas centre fixed in image space
        cx = self._canvas_size / 2.0
        cy = self._canvas_size / 2.0
        ratio = new_zoom / old_zoom
        self._img_x = cx - ratio * (cx - self._img_x)
        self._img_y = cy - ratio * (cy - self._img_y)
        self._zoom = new_zoom
        self._clamp()
        self.update()

    # --- private ---

    def _center_image(self) -> None:
        """Position the scaled image so it is centred in the canvas."""
        scaled_w = self._source.width() * self._zoom
        scaled_h = self._source.height() * self._zoom
        self._img_x = (self._canvas_size - scaled_w) / 2.0
        self._img_y = (self._canvas_size - scaled_h) / 2.0

    def _clamp(self) -> None:
        """Prevent the image from being panned so far that the crop circle leaves it."""
        cs = self._canvas_size
        scaled_w = self._source.width() * self._zoom
        scaled_h = self._source.height() * self._zoom
        # Image must cover the full canvas width/height so the circle is always inside
        self._img_x = min(0.0, max(cs - scaled_w, self._img_x))
        self._img_y = min(0.0, max(cs - scaled_h, self._img_y))


class SettingsDialog(QDialog):
    """Application settings dialog with Identity, Propagation, and Security tabs."""

    def __init__(self, config: Config, identity: Identity,
                 storage: Storage, router: Router,
                 avatar_mgr=None, subscriber_lookup=None,
                 parent=None):
        super().__init__(parent)
        self._config = config
        self._identity = identity
        self._storage = storage
        self._router = router
        self._avatar_mgr = avatar_mgr
        self._subscriber_lookup = subscriber_lookup or (lambda _: set())

        # Pending avatar state: None means no change; b"" means remove
        self._pending_avatar: bytes | None = None
        self._avatar_changed = False

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
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # --- Avatar section ---
        avatar_group = QGroupBox("Profile Picture")
        avatar_layout = QHBoxLayout(avatar_group)
        avatar_layout.setSpacing(16)

        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(_PREVIEW_SIZE, _PREVIEW_SIZE)
        self._avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._avatar_label.setStyleSheet(
            "border-radius: 40px; background: #2a2a2a;"
        )
        self._refresh_avatar_preview()
        avatar_layout.addWidget(self._avatar_label)

        btn_col = QVBoxLayout()
        choose_btn = QPushButton("Choose Image…")
        choose_btn.clicked.connect(self._on_choose_avatar)
        btn_col.addWidget(choose_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove_avatar)
        btn_col.addWidget(remove_btn)

        btn_col.addStretch()
        avatar_layout.addLayout(btn_col)
        avatar_layout.addStretch()
        layout.addWidget(avatar_group)

        # --- Identity fields ---
        form = QFormLayout()
        form.setSpacing(8)

        self._display_name_edit = QLineEdit(self._config.display_name)
        form.addRow("Display name:", self._display_name_edit)

        id_label = QLabel(self._identity.hash_hex)
        id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        id_label.setStyleSheet("color: #888; font-family: monospace; font-size: 11px;")
        form.addRow("Identity hash:", id_label)

        self._outbound_edit = QLineEdit(self._config.outbound_propagation_node or "")
        self._outbound_edit.setPlaceholderText("Leave blank to use direct delivery only")
        form.addRow("Propagation node:", self._outbound_edit)

        layout.addLayout(form)
        layout.addStretch()
        return widget

    def _refresh_avatar_preview(self) -> None:
        """Update the avatar preview label from pending state or config."""
        if self._avatar_changed and self._pending_avatar:
            pixmap = QPixmap()
            pixmap.loadFromData(self._pending_avatar)
        elif self._config.avatar_bytes:
            pixmap = QPixmap()
            pixmap.loadFromData(self._config.avatar_bytes)
        else:
            pixmap = None

        if pixmap and not pixmap.isNull():
            circular = _make_circular_pixmap(pixmap, _PREVIEW_SIZE)
            self._avatar_label.setPixmap(circular)
        else:
            self._avatar_label.setPixmap(QPixmap())
            self._avatar_label.setText("No photo")
            self._avatar_label.setStyleSheet(
                "border-radius: 40px; background: #2a2a2a; color: #666;"
                " font-size: 11px;"
            )

    def _on_choose_avatar(self) -> None:
        """Open file dialog, then show the crop dialog."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Profile Picture",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)",
        )
        if not path:
            return

        dlg = AvatarCropDialog(path, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.cropped_bytes:
            self._pending_avatar = dlg.cropped_bytes
            self._avatar_changed = True
            self._refresh_avatar_preview()

    def _on_remove_avatar(self) -> None:
        """Mark the avatar for removal."""
        self._pending_avatar = b""
        self._avatar_changed = True
        self._avatar_label.setPixmap(QPixmap())
        self._avatar_label.setText("No photo")
        self._avatar_label.setStyleSheet(
            "border-radius: 40px; background: #2a2a2a; color: #666; font-size: 11px;"
        )

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

        # Avatar
        if self._avatar_changed and self._avatar_mgr is not None:
            try:
                if self._pending_avatar:
                    self._avatar_mgr.set_avatar(
                        self._pending_avatar, self._subscriber_lookup
                    )
                else:
                    self._avatar_mgr.remove_avatar(self._subscriber_lookup)
            except RuntimeError as e:
                QMessageBox.warning(self, "Avatar Rate Limit", str(e))
            except Exception as e:
                QMessageBox.critical(self, "Avatar Error", str(e))

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
