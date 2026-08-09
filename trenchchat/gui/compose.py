"""
Message compose widget with send button, image attachment, Shift+Enter newline support,
and Discord-style :emoji_name: inline autocomplete.
"""

import re

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QTextEdit, QPushButton,
    QLabel, QFileDialog, QSizePolicy, QListWidget, QListWidgetItem, QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QSize
from PyQt6.QtGui import QKeyEvent, QIcon, QPixmap, QTextCursor

from trenchchat.core.storage import Storage
from trenchchat.gui import theme


_ATTACH_BTN_WIDTH = 38
_SEND_BTN_WIDTH = 38
_COMPOSE_HEIGHT = 38
_PREVIEW_MAX_PX = 80     # thumbnail max dimension in the preview area
_AUTOCOMPLETE_MAX = 8    # max emoji results shown in the autocomplete popup
_AUTOCOMPLETE_ICON_SIZE = 24  # px

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)"
# Characters that are valid inside a :name: token
_EMOJI_NAME_RE = re.compile(r":([a-zA-Z0-9_-]*)$")


class _EmojiAutocompletePopup(QListWidget):
    """Floating list of emoji completions shown while the user types :prefix."""

    emoji_chosen = pyqtSignal(str, str)  # (name, emoji_hash)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setStyleSheet(f"""
            QListWidget {{
                background: {theme.DIALOG_BG}; border: 1px solid {theme.BORDER}; color: {theme.TEXT};
                font-size: 12px; outline: none;
            }}
            QListWidget::item:selected {{ background: {theme.ACCENT_WASH_SELECTED}; color: {theme.ACCENT}; }}
            QListWidget::item:hover {{ background: {theme.BORDER_SOFT}; }}
        """)
        self.setIconSize(QSize(_AUTOCOMPLETE_ICON_SIZE, _AUTOCOMPLETE_ICON_SIZE))
        self.itemClicked.connect(self._on_item_clicked)

    def populate(self, rows: list) -> None:
        """Rebuild list from storage rows, each having emoji_hash / name / image_data."""
        self.clear()
        for row in rows[:_AUTOCOMPLETE_MAX]:
            item = QListWidgetItem(f"  :{row['name']}:")
            pix = QPixmap()
            pix.loadFromData(bytes(row["image_data"]))
            if not pix.isNull():
                item.setIcon(QIcon(pix.scaled(
                    _AUTOCOMPLETE_ICON_SIZE, _AUTOCOMPLETE_ICON_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )))
            item.setData(Qt.ItemDataRole.UserRole, (row["name"], row["emoji_hash"]))
            self.addItem(item)

        if self.count():
            self.setCurrentRow(0)

        row_height = _AUTOCOMPLETE_ICON_SIZE + 6
        self.setFixedHeight(min(self.count(), _AUTOCOMPLETE_MAX) * row_height + 4)

    def move_selection(self, delta: int) -> None:
        """Move the highlighted item up (-1) or down (+1)."""
        if not self.count():
            return
        current = self.currentRow()
        self.setCurrentRow(max(0, min(self.count() - 1, current + delta)))

    def accept_current(self) -> None:
        item = self.currentItem()
        if item:
            name, emoji_hash = item.data(Qt.ItemDataRole.UserRole)
            self.emoji_chosen.emit(name, emoji_hash)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name, emoji_hash = item.data(Qt.ItemDataRole.UserRole)
        self.emoji_chosen.emit(name, emoji_hash)


class ComposeBox(QTextEdit):
    """Text input that sends on Enter and inserts newline on Shift+Enter."""

    send_requested = pyqtSignal()
    emoji_query_changed = pyqtSignal(str)   # prefix after ':', or "" to dismiss

    def __init__(self, parent=None):
        super().__init__(parent)
        # Optional callback to forward navigation keys to an autocomplete popup.
        # ComposeWidget sets this when storage is provided.
        self._key_interceptor = None

    def set_key_interceptor(self, cb) -> None:
        """cb(event) -> bool: return True if the event was consumed."""
        self._key_interceptor = cb

    def keyPressEvent(self, event: QKeyEvent):
        if self._key_interceptor and self._key_interceptor(event):
            return
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.send_requested.emit()
                return
        super().keyPressEvent(event)
        # After every keystroke, check whether we are inside a :name: sequence
        self._update_emoji_query()

    def _update_emoji_query(self) -> None:
        """Emit emoji_query_changed with the current colon-prefix, or '' to dismiss."""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        line_up_to_cursor = cursor.selectedText()
        m = _EMOJI_NAME_RE.search(line_up_to_cursor)
        if m:
            self.emoji_query_changed.emit(m.group(1))
        else:
            self.emoji_query_changed.emit("")


class ComposeWidget(QWidget):
    """
    Bottom-of-window compose area.

    Emits message_ready(text, image_data) when the user sends.  image_data is
    raw bytes of the selected file, or None when no image is attached.  Either
    text or image_data (or both) will be non-empty/non-None.

    Pass *storage* to enable the :name: inline emoji autocomplete.
    """

    message_ready = pyqtSignal(str, object)

    def __init__(self, storage: Storage | None = None, parent=None):
        super().__init__(parent)
        self._pending_image: bytes | None = None
        self._storage = storage

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- Preview bar (hidden until an image is attached) ---
        self._preview_bar = QWidget()
        self._preview_bar.setStyleSheet(f"background: {theme.PANEL_BG};")
        self._preview_bar.hide()
        preview_layout = QHBoxLayout(self._preview_bar)
        preview_layout.setContentsMargins(16, 8, 16, 8)
        preview_layout.setSpacing(10)

        self._preview_thumb = QLabel()
        self._preview_thumb.setFixedSize(_PREVIEW_MAX_PX, _PREVIEW_MAX_PX)
        self._preview_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self._preview_thumb)

        self._preview_label = QLabel()
        self._preview_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11.5px;")
        self._preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        preview_layout.addWidget(self._preview_label)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(22, 22)
        remove_btn.setStyleSheet(f"""
            QPushButton {{
                background: {theme.INPUT_BG}; color: {theme.TEXT}; border: none; border-radius: 6px;
                padding: 0;
            }}
            QPushButton:hover {{ background: {theme.DANGER_BG}; color: #fff; }}
        """)
        remove_btn.clicked.connect(self._clear_image)
        preview_layout.addWidget(remove_btn)

        outer.addWidget(self._preview_bar)

        # --- Compose row ---
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(16, 10, 16, 10)
        row_layout.setSpacing(8)

        self._attach_btn = QPushButton("📎")
        self._attach_btn.setFixedWidth(_ATTACH_BTN_WIDTH)
        self._attach_btn.setFixedHeight(_COMPOSE_HEIGHT)
        self._attach_btn.setStyleSheet(f"""
            QPushButton {{
                background: {theme.INPUT_BG}; color: {theme.TEXT_MUTED}; font-size: 15px;
                border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 0;
            }}
            QPushButton:hover {{ background: {theme.BORDER_SOFT}; color: {theme.TEXT}; }}
            QPushButton:disabled {{ color: {theme.TEXT_FAINT}; }}
        """)
        self._attach_btn.clicked.connect(self._on_attach)
        row_layout.addWidget(self._attach_btn)

        self._editor = ComposeBox()
        self._editor.setPlaceholderText("Message…  (Enter to send, Shift+Enter for newline)")
        self._editor.setFixedHeight(_COMPOSE_HEIGHT)
        self._editor.send_requested.connect(self._on_send)
        self._editor.emoji_query_changed.connect(self._on_emoji_query)
        self._editor.focusOutEvent = self._on_editor_focus_out
        row_layout.addWidget(self._editor)

        self._send_btn = QPushButton("➤")
        self._send_btn.setFixedWidth(_SEND_BTN_WIDTH)
        self._send_btn.setFixedHeight(_COMPOSE_HEIGHT)
        self._send_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {theme.ACCENT}; font-size: 15px;
                border: 1px solid {theme.ACCENT}; border-radius: 8px; padding: 0;
            }}
            QPushButton:hover {{ background: {theme.ACCENT_WASH_HOVER}; }}
            QPushButton:disabled {{ color: {theme.TEXT_FAINT}; border-color: {theme.BORDER}; }}
        """)
        self._send_btn.clicked.connect(self._on_send)
        row_layout.addWidget(self._send_btn)

        outer.addWidget(row)

        # Autocomplete popup — created lazily on first query so that self.window()
        # resolves to the actual top-level window rather than the widget itself.
        self._autocomplete: _EmojiAutocompletePopup | None = None
        if self._storage is not None:
            self._editor.set_key_interceptor(self._forward_autocomplete_key)

    # ------------------------------------------------------------------
    # Emoji autocomplete
    # ------------------------------------------------------------------

    def _get_autocomplete(self) -> "_EmojiAutocompletePopup | None":
        """Return the autocomplete popup, creating it lazily on first call."""
        if self._storage is None:
            return None
        if self._autocomplete is None:
            self._autocomplete = _EmojiAutocompletePopup(self.window())
            self._autocomplete.emoji_chosen.connect(self._on_emoji_chosen)
            self._autocomplete.hide()
        return self._autocomplete

    def _on_emoji_query(self, prefix: str) -> None:
        """Show, update, or hide the autocomplete popup based on the current prefix."""
        if self._storage is None:
            return
        popup = self._get_autocomplete()
        if popup is None:
            return

        if not prefix:
            popup.hide()
            return

        rows = self._storage.search_emojis(prefix)
        if not rows:
            popup.hide()
            return

        popup.populate(rows)

        # Position the popup above the compose row
        editor_global = self._editor.mapToGlobal(QPoint(0, 0))
        popup_x = editor_global.x()
        popup_y = editor_global.y() - popup.height() - 4
        popup.move(popup_x, popup_y)
        popup.setFixedWidth(self._editor.width())
        popup.show()
        popup.raise_()

    def _on_emoji_chosen(self, name: str, emoji_hash: str) -> None:
        """Replace the active :prefix token with :name: in the editor."""
        popup = self._get_autocomplete()
        if popup:
            popup.hide()

        cursor = self._editor.textCursor()
        text_up_to_cursor = self._editor.toPlainText()[:cursor.position()]
        m = _EMOJI_NAME_RE.search(text_up_to_cursor)
        if not m:
            self._editor.setFocus()
            return

        # Remove the :prefix and insert :name:
        prefix_len = len(m.group(0))   # includes the leading ':'
        cursor.movePosition(
            QTextCursor.MoveOperation.Left,
            QTextCursor.MoveMode.KeepAnchor,
            prefix_len,
        )
        cursor.insertText(f":{name}@{emoji_hash}: ")
        self._editor.setTextCursor(cursor)
        self._editor.setFocus()

    def _on_editor_focus_out(self, event) -> None:
        """Hide the autocomplete popup when the editor loses keyboard focus."""
        if self._autocomplete is not None:
            self._autocomplete.hide()
        QTextEdit.focusOutEvent(self._editor, event)

    def _forward_autocomplete_key(self, event: QKeyEvent) -> bool:
        """Forward Up/Down/Enter/Tab/Escape to the autocomplete popup if visible.

        Returns True if the event was consumed.
        """
        popup = self._autocomplete  # use cached ref — don't create lazily on keypress
        if popup is None or not popup.isVisible():
            return False
        key = event.key()
        if key == Qt.Key.Key_Up:
            popup.move_selection(-1)
            return True
        if key == Qt.Key.Key_Down:
            popup.move_selection(1)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            popup.accept_current()
            return True
        if key == Qt.Key.Key_Escape:
            popup.hide()
            return True
        return False

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
