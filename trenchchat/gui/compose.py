"""
Message compose widget with send button and Shift+Enter newline support.
"""

from PyQt6.QtWidgets import QWidget, QHBoxLayout, QTextEdit, QPushButton
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent


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
    Emits message_ready(text) when the user sends.
    """

    message_ready = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(6)

        self._editor = ComposeBox()
        self._editor.setPlaceholderText("Message…  (Enter to send, Shift+Enter for newline)")
        self._editor.setFixedHeight(60)
        self._editor.send_requested.connect(self._on_send)
        layout.addWidget(self._editor)

        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedWidth(70)
        self._send_btn.setFixedHeight(60)
        self._send_btn.clicked.connect(self._on_send)
        layout.addWidget(self._send_btn)

    def _on_send(self):
        text = self._editor.toPlainText().strip()
        if text:
            self.message_ready.emit(text)
            self._editor.clear()

    def set_enabled(self, enabled: bool):
        self._editor.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)

    def set_placeholder(self, text: str):
        self._editor.setPlaceholderText(text)
