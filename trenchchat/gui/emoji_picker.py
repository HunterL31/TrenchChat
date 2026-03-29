"""
Emoji picker popup and emoji management dialog for TrenchChat.

EmojiPicker -- floating popup shown when the user clicks the react button on
               a message or triggers the inline autocomplete.  Displays a
               search box and a scrollable grid of custom emoji thumbnails.
               Emits emoji_selected(emoji_hash: str) when the user picks one.

EmojiImportDialog -- modal dialog for importing a new emoji image from disk
                     and assigning it a short name.
"""

import hashlib
import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QLabel, QScrollArea, QGridLayout, QDialog, QFileDialog,
    QMessageBox, QSizePolicy, QFrame,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap, QCursor

from trenchchat.core.storage import Storage
from trenchchat.core.reaction import MAX_EMOJI_BYTES, compute_emoji_hash

_THUMB_SIZE = 32          # px for emoji thumbnails in the picker grid
_COLS = 8                 # columns in the emoji grid
_MAX_POPUP_HEIGHT = 320   # px
_EMOJI_FILTER = "Images (*.png *.gif *.jpg *.jpeg *.webp)"


class _EmojiButton(QPushButton):
    """A square button showing a single emoji thumbnail."""

    def __init__(self, emoji_hash: str, image_data: bytes, name: str, parent=None):
        super().__init__(parent)
        self.emoji_hash = emoji_hash
        self.setFixedSize(_THUMB_SIZE + 4, _THUMB_SIZE + 4)
        self.setToolTip(f":{name}:")
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 4px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.12); }"
        )
        pix = QPixmap()
        pix.loadFromData(image_data)
        if not pix.isNull():
            scaled = pix.scaled(
                _THUMB_SIZE, _THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.setIcon(scaled)
            self.setIconSize(QSize(_THUMB_SIZE, _THUMB_SIZE))
        else:
            self.setText("?")


class EmojiPicker(QFrame):
    """Floating emoji picker popup.

    Positioned by the caller above or near a message row.  Emits
    ``emoji_selected(emoji_hash)`` when the user clicks an emoji.
    Dismiss by hiding the widget.
    """

    emoji_selected = pyqtSignal(str)   # emoji_hash hex

    def __init__(self, storage: Storage, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self._storage = storage
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "EmojiPicker { background: #2a2a2a; border: 1px solid #555; border-radius: 6px; }"
        )
        self.setFixedWidth(_COLS * (_THUMB_SIZE + 4) + 24)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Search box
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search emojis…")
        self._search.setStyleSheet(
            "QLineEdit { background: #1e1e1e; color: #ddd; border: 1px solid #555; "
            "border-radius: 4px; padding: 4px 6px; font-size: 12px; }"
        )
        self._search.textChanged.connect(self._on_search)
        outer.addWidget(self._search)

        # Scrollable emoji grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.setMaximumHeight(_MAX_POPUP_HEIGHT)

        self._grid_container = QWidget()
        self._grid_container.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._grid_container)
        self._grid.setSpacing(2)
        self._grid.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(self._grid_container)
        outer.addWidget(scroll)

        # Footer: import button
        import_btn = QPushButton("+ Import Emoji")
        import_btn.setStyleSheet(
            "QPushButton { background: #333; color: #aaa; border: none; "
            "border-radius: 4px; padding: 4px 8px; font-size: 11px; }"
            "QPushButton:hover { background: #444; color: #fff; }"
        )
        import_btn.clicked.connect(self._on_import)
        outer.addWidget(import_btn)

        self._populate("")

    def focus_search(self) -> None:
        """Set keyboard focus to the search box."""
        self._search.setFocus()
        self._search.clear()

    def _on_search(self, text: str) -> None:
        self._populate(text.strip())

    def _populate(self, query: str) -> None:
        """Rebuild the emoji grid from storage results matching *query*."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        rows = self._storage.search_emojis(query) if query else self._storage.list_emojis()
        for i, row in enumerate(rows):
            btn = _EmojiButton(row["emoji_hash"], bytes(row["image_data"]), row["name"])
            btn.clicked.connect(
                lambda checked=False, h=row["emoji_hash"]: self._on_emoji_clicked(h)
            )
            self._grid.addWidget(btn, i // _COLS, i % _COLS)

        if not rows:
            lbl = QLabel("No emojis yet" if not query else "No matches")
            lbl.setStyleSheet("color: #777; font-size: 11px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid.addWidget(lbl, 0, 0, 1, _COLS)

    def _on_emoji_clicked(self, emoji_hash: str) -> None:
        self.emoji_selected.emit(emoji_hash)
        self.hide()

    def _on_import(self) -> None:
        dlg = EmojiImportDialog(self._storage, self)
        if dlg.exec():
            self._populate(self._search.text().strip())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)


class EmojiImportDialog(QDialog):
    """Modal dialog for importing a new custom emoji from disk.

    The user selects an image file and enters a short name.  On accept the
    emoji is written to the local ``custom_emojis`` table via *storage*.
    """

    def __init__(self, storage: Storage, parent=None):
        super().__init__(parent)
        self._storage = storage
        self._image_data: bytes | None = None
        self._emoji_hash: str | None = None

        self.setWindowTitle("Import Emoji")
        self.setModal(True)
        self.setMinimumWidth(320)
        self.setStyleSheet("background: #2a2a2a; color: #ddd;")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # Image pick row
        img_row = QHBoxLayout()
        self._img_label = QLabel("No image selected")
        self._img_label.setStyleSheet("color: #aaa; font-size: 11px;")
        self._img_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        img_row.addWidget(self._img_label)

        pick_btn = QPushButton("Choose File…")
        pick_btn.setStyleSheet(
            "QPushButton { background: #3a3a3a; color: #ccc; border: 1px solid #555; "
            "border-radius: 4px; padding: 4px 10px; }"
            "QPushButton:hover { background: #4a4a4a; }"
        )
        pick_btn.clicked.connect(self._on_pick)
        img_row.addWidget(pick_btn)
        layout.addLayout(img_row)

        # Preview
        self._preview = QLabel()
        self._preview.setFixedSize(64, 64)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet("background: #1e1e1e; border-radius: 4px;")
        layout.addWidget(self._preview, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Name field
        name_lbl = QLabel("Short name (e.g. salute, pepe):")
        name_lbl.setStyleSheet("font-size: 12px;")
        layout.addWidget(name_lbl)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("emoji_name")
        self._name_edit.setStyleSheet(
            "QLineEdit { background: #1e1e1e; color: #ddd; border: 1px solid #555; "
            "border-radius: 4px; padding: 4px 6px; font-size: 12px; }"
        )
        layout.addWidget(self._name_edit)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(
            "QPushButton { background: #333; color: #aaa; border: 1px solid #555; "
            "border-radius: 4px; padding: 4px 14px; }"
            "QPushButton:hover { background: #444; }"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._ok_btn = QPushButton("Import")
        self._ok_btn.setDefault(True)
        self._ok_btn.setEnabled(False)
        self._ok_btn.setStyleSheet(
            "QPushButton { background: #4a90d9; color: #fff; border: none; "
            "border-radius: 4px; padding: 4px 14px; }"
            "QPushButton:hover { background: #5aa0e9; }"
            "QPushButton:disabled { background: #333; color: #666; }"
        )
        self._ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

    def _on_pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Emoji Image", "", _EMOJI_FILTER)
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            QMessageBox.warning(self, "Error", f"Could not read file: {e}")
            return

        if len(data) > MAX_EMOJI_BYTES:
            QMessageBox.warning(
                self, "File Too Large",
                f"Emoji image must be under {MAX_EMOJI_BYTES // 1024} KB. "
                f"Selected file is {len(data) // 1024} KB.",
            )
            return

        self._image_data = data
        self._emoji_hash = compute_emoji_hash(data)

        pix = QPixmap()
        pix.loadFromData(data)
        if not pix.isNull():
            self._preview.setPixmap(
                pix.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            )
        else:
            self._preview.setText("?")

        name = os.path.splitext(os.path.basename(path))[0]
        if not self._name_edit.text():
            self._name_edit.setText(name.lower().replace(" ", "_"))

        self._img_label.setText(f"{os.path.basename(path)}  ({len(data) // 1024} KB)")
        self._ok_btn.setEnabled(True)

    def _on_ok(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Name Required", "Please enter a short name for the emoji.")
            return
        if self._image_data is None or self._emoji_hash is None:
            return

        if self._storage.emoji_exists(self._emoji_hash):
            QMessageBox.information(self, "Already Imported",
                                    "This emoji image is already in your library.")
            self.accept()
            return

        self._storage.insert_emoji(
            self._emoji_hash, name, self._image_data, __import__("time").time()
        )
        self.accept()
