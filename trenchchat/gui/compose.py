"""
Message compose widget with send button, image attachment, and Shift+Enter newline support.
"""

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QTextEdit, QPushButton,
    QLabel, QFileDialog, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QPixmap


_ATTACH_BTN_WIDTH = 36
_SEND_BTN_WIDTH = 70
_COMPOSE_HEIGHT = 60
_PREVIEW_MAX_PX = 80     # thumbnail max dimension in the preview area

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)"


class ComposeBox(QTextEdit):
    """Text input that sends on Enter and inserts newline on Shift+Enter."""

    send_requested = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.send_requested.emit()
        else:
            super().keyPressEvent(event)


class ComposeWidget(QWidget):
    """
    Bottom-of-window compose area.

    Emits message_ready(text, image_data) when the user sends.  image_data is
    raw bytes of the selected file, or None when no image is attached.  Either
    text or image_data (or both) will be non-empty/non-None.
    """

    message_ready = pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending_image: bytes | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- Preview bar (hidden until an image is attached) ---
        self._preview_bar = QWidget()
        self._preview_bar.setStyleSheet("background: #2a2a2a;")
        self._preview_bar.hide()
        preview_layout = QHBoxLayout(self._preview_bar)
        preview_layout.setContentsMargins(8, 4, 8, 4)
        preview_layout.setSpacing(8)

        self._preview_thumb = QLabel()
        self._preview_thumb.setFixedSize(_PREVIEW_MAX_PX, _PREVIEW_MAX_PX)
        self._preview_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self._preview_thumb)

        self._preview_label = QLabel()
        self._preview_label.setStyleSheet("color: #aaa; font-size: 11px;")
        self._preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        preview_layout.addWidget(self._preview_label)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(24, 24)
        remove_btn.setStyleSheet(
            "QPushButton { background: #444; color: #ccc; border: none; border-radius: 4px; }"
            "QPushButton:hover { background: #c0392b; color: #fff; }"
        )
        remove_btn.clicked.connect(self._clear_image)
        preview_layout.addWidget(remove_btn)

        outer.addWidget(self._preview_bar)

        # --- Compose row ---
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 4, 8, 8)
        row_layout.setSpacing(6)

        self._attach_btn = QPushButton("+")
        self._attach_btn.setFixedWidth(_ATTACH_BTN_WIDTH)
        self._attach_btn.setFixedHeight(_COMPOSE_HEIGHT)
        self._attach_btn.setStyleSheet(
            "QPushButton { background: #2a2a2a; color: #aaa; font-size: 20px; "
            "border: 1px solid #444; border-radius: 4px; }"
            "QPushButton:hover { background: #3a3a3a; color: #fff; }"
            "QPushButton:disabled { color: #555; }"
        )
        self._attach_btn.clicked.connect(self._on_attach)
        row_layout.addWidget(self._attach_btn)

        self._editor = ComposeBox()
        self._editor.setPlaceholderText("Message…  (Enter to send, Shift+Enter for newline)")
        self._editor.setFixedHeight(_COMPOSE_HEIGHT)
        self._editor.send_requested.connect(self._on_send)
        row_layout.addWidget(self._editor)

        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedWidth(_SEND_BTN_WIDTH)
        self._send_btn.setFixedHeight(_COMPOSE_HEIGHT)
        self._send_btn.clicked.connect(self._on_send)
        row_layout.addWidget(self._send_btn)

        outer.addWidget(row)

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _on_attach(self):
        """Open file dialog and store the selected image bytes."""
        path, _ = QFileDialog.getOpenFileName(self, "Attach Image", "", _IMAGE_FILTER)
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return
        self._pending_image = data
        self._show_preview(path, data)

    def _show_preview(self, path: str, data: bytes) -> None:
        """Populate and show the attachment preview bar."""
        pix = QPixmap()
        pix.loadFromData(data)
        if not pix.isNull():
            scaled = pix.scaled(
                _PREVIEW_MAX_PX, _PREVIEW_MAX_PX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._preview_thumb.setPixmap(scaled)
        else:
            self._preview_thumb.setText("?")

        import os
        name = os.path.basename(path)
        size_kb = len(data) / 1024
        self._preview_label.setText(f"{name}  ({size_kb:.0f} KB)")
        self._preview_bar.show()

    def _clear_image(self):
        """Remove the pending image attachment."""
        self._pending_image = None
        self._preview_thumb.clear()
        self._preview_label.clear()
        self._preview_bar.hide()

    def _on_send(self):
        text = self._editor.toPlainText().strip()
        image = self._pending_image
        if not text and not image:
            return
        self.message_ready.emit(text, image)
        self._editor.clear()
        self._clear_image()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool):
        """Enable or disable the entire compose area."""
        self._editor.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)
        self._attach_btn.setEnabled(enabled)

    def set_placeholder(self, text: str):
        """Update the placeholder text in the editor."""
        self._editor.setPlaceholderText(text)
