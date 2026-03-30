"""
Per-channel message display widget.

Shows messages sorted by timestamp with causal tiebreaking via last_seen_id.
Late-arriving messages are flagged visually.

Layout mirrors Discord: avatar on the left, sender name + timestamp on one
line, message text below.  Consecutive messages from the same sender within
GROUP_WINDOW_SECS are grouped — only the first shows the avatar/name header;
subsequent ones show only the indented text.  Hovering a continuation row
reveals a faint timestamp to the left of the text.
"""

import base64
import datetime
import hashlib
import html
import re
import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QSizePolicy, QDialog,
    QPushButton,
)
from PyQt6.QtCore import Qt, QTimer, QBuffer, QByteArray, QIODevice, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPixmap, QPainter, QPainterPath, QMovie

from trenchchat.core.reaction import ReactionManager
from trenchchat.core.storage import Storage

# Messages received more than this many seconds after their timestamp are "late"
LATE_THRESHOLD_SECS = 30.0

_MESSAGE_HISTORY_LIMIT = 500
_AVATAR_SIZE = 40              # avatar circle diameter in pixels
_ROW_LEFT_PAD = 12             # padding left of avatar
_ROW_RIGHT_PAD = 36            # padding right edge — wide enough to hold the react button
_ROW_V_PAD = 4                 # vertical padding for header rows
_ROW_V_PAD_CONT = 1            # vertical padding for continuation rows
_AVATAR_TEXT_GAP = 10          # gap between avatar column and text column
# Seconds within which consecutive messages from the same sender are grouped
GROUP_WINDOW_SECS = 300
# Max width of an inline image thumbnail (height scales proportionally)
_INLINE_IMAGE_MAX_PX = 400
# Number of times an inline GIF loops before freezing on the last frame
_GIF_INLINE_LOOPS = 2


_INLINE_EMOJI_PX = 20   # height of inline emoji images in message text

# Matches :name@hexhash: (new unambiguous format) or :name: (legacy).
# Group 1 = name, group 2 = 64-char hex hash (may be absent for legacy tokens).
_EMOJI_TOKEN_RE = re.compile(
    r":([a-zA-Z0-9_-]+)(?:@([0-9a-fA-F]{64}))?:"
)


def _render_content(
    content: str,
    storage: Storage,
    reaction_mgr: "ReactionManager | None" = None,
    sender_hex: str = "",
) -> tuple[str, bool]:
    """Convert message content to a displayable form.

    Tokens take two forms:
      :name@hexhash:  — unambiguous; look up by the 64-char SHA-256 hash
      :name:          — legacy; fall back to a name-based exact match

    When *reaction_mgr* and *sender_hex* are provided, any token whose emoji
    is not found locally triggers a background fetch request to the sender.
    """
    if ":" not in content:
        return content, False

    parts: list[str] = []
    last = 0
    found_any = False

    for m in _EMOJI_TOKEN_RE.finditer(content):
        name = m.group(1)
        emoji_hash = m.group(2)  # None for legacy :name: tokens

        if emoji_hash:
            row = storage.get_emoji(emoji_hash)
        else:
            # Legacy :name: token — exact name match
            rows = storage.search_emojis(name)
            row = next((r for r in rows if r["name"] == name), None)
            if row:
                emoji_hash = row["emoji_hash"]

        if row is None:
            # Not in local library — request it from the sender by hash,
            # passing the name so the sender echoes it back in the response.
            if reaction_mgr is not None and sender_hex and emoji_hash:
                reaction_mgr.request_emoji(sender_hex, emoji_hash, name=name)
            continue

        found_any = True
        parts.append(html.escape(content[last:m.start()]))
        img_bytes = bytes(row["image_data"])
        b64 = base64.b64encode(img_bytes).decode()
        mime = "image/gif" if img_bytes[:3] == b"GIF" else "image/png"
        # Wrap in a <big> tag whose font-size matches the image height so Qt's
        # rich-text engine allocates a line box tall enough to avoid clipping.
        parts.append(
            f'<span style="font-size:{_INLINE_EMOJI_PX}px">'
            f'<img src="data:{mime};base64,{b64}" '
            f'height="{_INLINE_EMOJI_PX}" '
            f'title=":{name}:" style="vertical-align:middle"/>'
            f'</span>'
        )
        last = m.end()

    if not found_any:
        return content, False

    parts.append(html.escape(content[last:]))
    return "".join(parts), True


def _format_ts(ts: float) -> str:
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%b %d %Y %H:%M")


def _format_ts_short(ts: float) -> str:
    """Short HH:MM used for the hover timestamp on continuation rows."""
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def _make_circular_pixmap(pixmap: QPixmap, size: int) -> QPixmap:
    """Return a size×size pixmap with the source rendered inside a circle."""
    scaled = pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
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


def _make_placeholder_pixmap(identity_hex: str, display_name: str, size: int) -> QPixmap:
    """Return a coloured circle with the first letter of the display name."""
    digest = hashlib.md5(identity_hex.encode()).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    color = QColor.fromHsv(hue, 150, 190)

    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    painter.fillPath(path, color)

    letter = (display_name[:1] or "?").upper()
    font = painter.font()
    font.setPointSize(max(8, size // 2 - 2))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(result.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return result


def _name_color(identity_hex: str, is_own: bool) -> str:
    """Return a CSS colour string for a sender's display name."""
    if is_own:
        return "#7eb8f7"
    digest = hashlib.md5(identity_hex.encode()).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    c = QColor.fromHsv(hue, 180, 220)
    return c.name()


def _make_qmovie(image_data: bytes) -> QMovie:
    """Wrap raw GIF bytes in a QBuffer and return a QMovie ready to play."""
    buf = QBuffer()
    buf.setData(QByteArray(image_data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    movie = QMovie()
    movie.setDevice(buf)
    movie._buf = buf  # keep buffer alive for the lifetime of the movie
    return movie


_GIF_MAGIC = (b"GIF87a", b"GIF89a")


class _ImageOverlay(QDialog):
    """Frameless full-window overlay; click outside the image to dismiss.

    Supports both static images and animated GIFs.  GIFs play on a loop
    while the overlay is open.
    """

    def __init__(self, image_data: bytes, parent=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)

        self._backdrop = QLabel(self)
        self._backdrop.setStyleSheet("background: rgba(0, 0, 0, 180);")

        self._is_gif = image_data[:6] in _GIF_MAGIC

        if self._is_gif:
            self._movie = _make_qmovie(image_data)
            self._movie.setCacheMode(QMovie.CacheMode.CacheAll)
            self._img_widget = QLabel(self)
            self._img_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._img_widget.setMovie(self._movie)
        else:
            pix = QPixmap()
            pix.loadFromData(bytes(image_data))
            self._full_pixmap = pix
            self._img_widget = QLabel(self)
            self._img_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def showEvent(self, event):
        self._layout_contents()
        super().showEvent(event)

    def resizeEvent(self, event):
        self._layout_contents()
        super().resizeEvent(event)

    def _layout_contents(self):
        self._backdrop.setGeometry(self.rect())
        max_w = max(1, int(self.width() * 0.9))
        max_h = max(1, int(self.height() * 0.9))

        if self._is_gif:
            self._movie.jumpToFrame(0)
            natural = self._movie.currentPixmap().size()
            if natural.isEmpty():
                QTimer.singleShot(50, self._layout_contents)
                return
            scale = min(max_w / natural.width(), max_h / natural.height(), 1.0)
            from PyQt6.QtCore import QSize
            self._movie.setScaledSize(
                QSize(int(natural.width() * scale), int(natural.height() * scale))
            )
            sw = int(natural.width() * scale)
            sh = int(natural.height() * scale)
            self._img_widget.resize(sw, sh)
            self._img_widget.move(
                (self.width() - sw) // 2,
                (self.height() - sh) // 2,
            )
            self._movie.start()
        else:
            scaled = self._full_pixmap.scaled(
                max_w, max_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_widget.setPixmap(scaled)
            self._img_widget.resize(scaled.width(), scaled.height())
            self._img_widget.move(
                (self.width() - scaled.width()) // 2,
                (self.height() - scaled.height()) // 2,
            )

    def mousePressEvent(self, event):
        if not self._img_widget.geometry().contains(event.pos()):
            if self._is_gif:
                self._movie.stop()
            self.close()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if self._is_gif:
                self._movie.stop()
            self.close()
        else:
            super().keyPressEvent(event)


class _AnimatedGifLabel(QLabel):
    """Inline GIF thumbnail that loops _GIF_INLINE_LOOPS times then freezes.

    Clicking resumes the inline animation and also opens the full overlay
    where the GIF loops continuously.  After the overlay closes the inline
    animation restarts from the beginning.
    """

    def __init__(self, image_data: bytes, parent=None):
        super().__init__(parent)
        self._image_data = image_data
        self._loop_count = 0
        self._prev_frame = -1
        self._frozen = False

        self._movie = _make_qmovie(image_data)
        self._movie.setCacheMode(QMovie.CacheMode.CacheAll)

        # Scale to thumbnail width
        self._movie.jumpToFrame(0)
        natural = self._movie.currentPixmap().size()
        if not natural.isEmpty() and natural.width() > _INLINE_IMAGE_MAX_PX:
            scale = _INLINE_IMAGE_MAX_PX / natural.width()
            from PyQt6.QtCore import QSize
            self._movie.setScaledSize(
                QSize(int(natural.width() * scale), int(natural.height() * scale))
            )

        self.setMovie(self._movie)
        self._movie.frameChanged.connect(self._on_frame_changed)
        self._movie.start()

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to replay / view full size")

    def _on_frame_changed(self, frame_number: int):
        if self._frozen:
            return
        # Frame wrapped back to 0 → one full loop completed
        if frame_number == 0 and self._prev_frame > 0:
            self._loop_count += 1
            if self._loop_count >= _GIF_INLINE_LOOPS:
                last = self._movie.frameCount() - 1
                if last >= 0:
                    self._movie.jumpToFrame(last)
                self._movie.stop()
                self._frozen = True
        self._prev_frame = frame_number

    def _resume(self):
        """Restart the inline animation for another _GIF_INLINE_LOOPS loops."""
        self._loop_count = 0
        self._prev_frame = -1
        self._frozen = False
        self._movie.jumpToFrame(0)
        self._movie.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_full_view()
        super().mousePressEvent(event)

    def _open_full_view(self):
        """Show the GIF in a full-window overlay then restart inline on close."""
        top = self.window()
        overlay = _ImageOverlay(self._image_data, top)
        overlay.move(top.mapToGlobal(top.rect().topLeft()))
        overlay.resize(top.size())
        overlay.exec()
        self._resume()


class _ClickableImageLabel(QLabel):
    """Static image thumbnail (JPEG/PNG) that opens a full-size overlay on click."""

    def __init__(self, pixmap: QPixmap, image_data: bytes, parent=None):
        super().__init__(parent)
        self._image_data = image_data
        scaled = pixmap.scaledToWidth(
            min(_INLINE_IMAGE_MAX_PX, pixmap.width()),
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to view full size")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_full_view()
        super().mousePressEvent(event)

    def _open_full_view(self):
        top = self.window()
        overlay = _ImageOverlay(self._image_data, top)
        overlay.move(top.mapToGlobal(top.rect().topLeft()))
        overlay.resize(top.size())
        overlay.exec()


def _build_image_widget(image_data: bytes | None) -> QWidget | None:
    """Return the appropriate inline widget for the given image bytes, or None."""
    if not image_data:
        return None
    data = bytes(image_data)
    if data[:6] in _GIF_MAGIC:
        return _AnimatedGifLabel(data)
    pix = QPixmap()
    pix.loadFromData(data)
    if pix.isNull():
        return None
    return _ClickableImageLabel(pix, data)


_REACT_BTN_SIZE = 22        # reaction button diameter in px
_CHIP_EMOJI_SIZE = 18       # emoji thumbnail size inside a reaction chip
_CHIP_MAX_EMOJIS = 20       # maximum distinct emojis to show per message


class _ReactionChip(QPushButton):
    """A single reaction chip: emoji thumbnail + reactor count.

    Highlighted when the local user is one of the reactors.
    Clicking the chip emits ``toggled(emoji_hash, currently_reacted)``
    so the parent can add or remove the reaction.
    """

    toggled_reaction = pyqtSignal(str, bool)   # (emoji_hash, currently_reacted_by_user)

    def __init__(self, emoji_hash: str, image_data: bytes | None,
                 count: int, user_reacted: bool, parent=None):
        super().__init__(parent)
        self._emoji_hash = emoji_hash
        self._user_reacted = user_reacted

        pix = QPixmap()
        if image_data:
            pix.loadFromData(image_data)
        icon_str = ""
        if not pix.isNull():
            scaled = pix.scaled(
                _CHIP_EMOJI_SIZE, _CHIP_EMOJI_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.setIcon(QIcon(scaled))
            from PyQt6.QtCore import QSize
            self.setIconSize(QSize(_CHIP_EMOJI_SIZE, _CHIP_EMOJI_SIZE))
        else:
            icon_str = "?"

        label = f"{icon_str} {count}" if icon_str else str(count)
        self.setText(label)

        bg = "#3a5080" if user_reacted else "#2a2a2a"
        border = "#5a80c0" if user_reacted else "#444"
        self.setStyleSheet(
            f"QPushButton {{ background: {bg}; color: #ddd; border: 1px solid {border}; "
            f"border-radius: 10px; padding: 1px 6px; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {'#4a6090' if user_reacted else '#3a3a3a'}; }}"
        )
        self.clicked.connect(self._on_click)

    def _on_click(self) -> None:
        self.toggled_reaction.emit(self._emoji_hash, self._user_reacted)


class _ReactionBar(QWidget):
    """Horizontal flow of reaction chips beneath a message.

    Rebuild by calling ``refresh(reactions, storage, own_hash)``.
    """

    reaction_toggled = pyqtSignal(str, bool)   # (emoji_hash, currently_reacted_by_user)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 2, 0, 0)
        self._layout.setSpacing(4)
        self._layout.addStretch()
        self.hide()

    def refresh(self, message_id: str, storage: Storage, own_hash: str) -> None:
        """Rebuild chips from the current reactions for *message_id*."""
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        rows = storage.get_reactions(message_id)
        if not rows:
            self.hide()
            return

        # Group by emoji_hash
        counts: dict[str, int] = {}
        user_reacted: dict[str, bool] = {}
        ordered: list[str] = []
        for r in rows:
            eh = r["emoji_hash"]
            if eh not in counts:
                counts[eh] = 0
                user_reacted[eh] = False
                ordered.append(eh)
            counts[eh] += 1
            if r["reactor_hash"] == own_hash:
                user_reacted[eh] = True

        for eh in ordered[:_CHIP_MAX_EMOJIS]:
            row = storage.get_emoji(eh)
            img_data = bytes(row["image_data"]) if row else None
            chip = _ReactionChip(eh, img_data, counts[eh], user_reacted[eh])
            chip.toggled_reaction.connect(self.reaction_toggled)
            self._layout.insertWidget(self._layout.count() - 1, chip)

        self.show()


class _AvatarWidget(QWidget):
    """Fixed-size widget that paints a circular avatar without stylesheet cascade."""

    def __init__(self, size: int, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._pixmap: QPixmap | None = None

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event):
        if self._pixmap and not self._pixmap.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.drawPixmap(0, 0, self._pixmap)
            painter.end()


class MessageBubble(QWidget):
    """Discord-style header message row: circular avatar, sender name, timestamp, text.

    This is used for the first message in a group.  Subsequent messages from
    the same sender within GROUP_WINDOW_SECS use MessageContinuation instead.
    """

    react_requested = pyqtSignal(str)   # message_id — user clicked the react button

    def __init__(self, sender: str, sender_hash: str, content: str, timestamp: float,
                 received_at: float, message_id: str = "", is_own: bool = False,
                 avatar_pixmap: QPixmap | None = None,
                 image_data: bytes | None = None,
                 storage: Storage | None = None,
                 reaction_mgr: ReactionManager | None = None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._sender_hash = sender_hash
        self._message_id = message_id

        row = QHBoxLayout(self)
        row.setContentsMargins(_ROW_LEFT_PAD, _ROW_V_PAD, _ROW_RIGHT_PAD, _ROW_V_PAD)
        row.setSpacing(_AVATAR_TEXT_GAP)
        row.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Avatar
        self._avatar_widget = _AvatarWidget(_AVATAR_SIZE, self)
        self._set_avatar_pixmap(avatar_pixmap, sender, sender_hash)
        row.addWidget(self._avatar_widget, 0, Qt.AlignmentFlag.AlignTop)

        # Text column
        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)

        name_color = _name_color(sender_hash, is_own)
        hash_short = sender_hash[:8]
        header_html = (
            f"<span style='color:{name_color};font-weight:600'>{sender}</span>"
            f"&nbsp;<span style='color:#555;font-size:10px'>[{hash_short}]</span>"
            f"&nbsp;&nbsp;<span style='color:#555;font-size:10px'>{_format_ts(timestamp)}</span>"
        )
        header = QLabel(header_html)
        header.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(header)

        if content:
            if storage is not None:
                body_text, is_rich = _render_content(
                    content, storage, reaction_mgr, sender_hash
                )
            else:
                body_text, is_rich = content, False
            body = QLabel()
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if is_rich:
                body.setTextFormat(Qt.TextFormat.RichText)
                body.setText(
                    f'<span style="color:#dcddde;font-size:13px">{body_text}</span>'
                )
            else:
                body.setStyleSheet("color: #dcddde; font-size: 13px;")
                body.setText(body_text)
            col.addWidget(body)

        img_widget = _build_image_widget(image_data)
        if img_widget:
            col.addWidget(img_widget)

        if received_at - timestamp > LATE_THRESHOLD_SECS:
            late = QLabel("⟳ received late")
            late.setStyleSheet("color: #666; font-size: 10px; font-style: italic;")
            col.addWidget(late)

        self._reaction_bar = _ReactionBar()
        self._reaction_bar.reaction_toggled.connect(
            lambda eh, reacted: self.react_requested.emit(self._message_id)
            if not reacted else self._on_remove_reaction(eh)
        )
        col.addWidget(self._reaction_bar)

        row.addLayout(col, 1)

        # Hover react button — shown top-right on enterEvent
        self._react_btn = QPushButton("😊", self)
        self._react_btn.setFixedSize(_REACT_BTN_SIZE, _REACT_BTN_SIZE)
        self._react_btn.setStyleSheet(
            "QPushButton { background: #333; color: #aaa; border: 1px solid #555; "
            "border-radius: 11px; font-size: 11px; }"
            "QPushButton:hover { background: #444; color: #fff; }"
        )
        self._react_btn.setToolTip("Add reaction")
        self._react_btn.hide()
        self._react_btn.clicked.connect(
            lambda: self.react_requested.emit(self._message_id)
        )

        # store for _on_remove_reaction signal routing
        self._reaction_remove_cb = None

    def set_reaction_remove_callback(self, cb) -> None:
        """cb(message_id, emoji_hash) -- called when user clicks a reacted chip."""
        self._reaction_remove_cb = cb

    def _on_remove_reaction(self, emoji_hash: str) -> None:
        if self._reaction_remove_cb:
            self._reaction_remove_cb(self._message_id, emoji_hash)

    def refresh_reactions(self, storage: Storage, own_hash: str) -> None:
        """Rebuild the reaction chip bar from current DB state."""
        self._reaction_bar.refresh(self._message_id, storage, own_hash)

    def update_avatar(self, avatar_pixmap: QPixmap | None,
                      display_name: str) -> None:
        """Replace the avatar image when a new one arrives."""
        self._set_avatar_pixmap(avatar_pixmap, display_name, self._sender_hash)

    def _set_avatar_pixmap(self, avatar_pixmap: QPixmap | None,
                           display_name: str, sender_hash: str) -> None:
        if avatar_pixmap and not avatar_pixmap.isNull():
            pix = _make_circular_pixmap(avatar_pixmap, _AVATAR_SIZE)
        else:
            pix = _make_placeholder_pixmap(sender_hash, display_name, _AVATAR_SIZE)
        self._avatar_widget.set_pixmap(pix)

    def _react_btn_x(self) -> int:
        return self.width() - _REACT_BTN_SIZE - (_ROW_RIGHT_PAD - _REACT_BTN_SIZE) // 2

    def resizeEvent(self, event):
        self._react_btn.move(self._react_btn_x(), 4)
        super().resizeEvent(event)

    def enterEvent(self, event):
        self.setAutoFillBackground(True)
        p = self.palette()
        p.setColor(self.backgroundRole(), QColor(255, 255, 255, 12))
        self.setPalette(p)
        self._react_btn.move(self._react_btn_x(), 4)
        self._react_btn.raise_()
        self._react_btn.show()

    def leaveEvent(self, event):
        self.setAutoFillBackground(False)
        self._react_btn.hide()


class MessageContinuation(QWidget):
    """Grouped follow-on message row with no avatar or name header.

    Indented to align with the text column of the preceding MessageBubble.
    On hover, a faint HH:MM timestamp appears to the left of the message text
    and the row gets a subtle background highlight.
    """

    react_requested = pyqtSignal(str)   # message_id

    # Width of the "gutter" that replaces the avatar column so text aligns.
    # Must equal _ROW_LEFT_PAD + _AVATAR_SIZE + _AVATAR_TEXT_GAP.
    _GUTTER = _ROW_LEFT_PAD + _AVATAR_SIZE + _AVATAR_TEXT_GAP

    def __init__(self, sender_hash: str, content: str, timestamp: float,
                 received_at: float, message_id: str = "",
                 image_data: bytes | None = None,
                 storage: Storage | None = None,
                 reaction_mgr: ReactionManager | None = None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._message_id = message_id
        self._reaction_remove_cb = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, _ROW_V_PAD_CONT, _ROW_RIGHT_PAD, _ROW_V_PAD_CONT)
        row.setSpacing(0)
        row.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Timestamp label — hidden by default, revealed on hover
        self._ts_label = QLabel(_format_ts_short(timestamp))
        self._ts_label.setFixedWidth(self._GUTTER)
        self._ts_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._ts_label.setStyleSheet(
            "color: transparent; font-size: 10px; padding-right: 6px;"
        )
        row.addWidget(self._ts_label, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)

        if content:
            if storage is not None:
                body_text, is_rich = _render_content(
                    content, storage, reaction_mgr, sender_hash
                )
            else:
                body_text, is_rich = content, False
            self._body = QLabel()
            self._body.setWordWrap(True)
            self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if is_rich:
                self._body.setTextFormat(Qt.TextFormat.RichText)
                self._body.setText(
                    f'<span style="color:#dcddde;font-size:13px">{body_text}</span>'
                )
            else:
                self._body.setStyleSheet("color: #dcddde; font-size: 13px;")
                self._body.setText(body_text)
            col.addWidget(self._body)

        img_widget = _build_image_widget(image_data)
        if img_widget:
            col.addWidget(img_widget)

        if received_at - timestamp > LATE_THRESHOLD_SECS:
            late = QLabel("⟳ received late")
            late.setStyleSheet("color: #666; font-size: 10px; font-style: italic;")
            col.addWidget(late)

        self._reaction_bar = _ReactionBar()
        self._reaction_bar.reaction_toggled.connect(
            lambda eh, reacted: self.react_requested.emit(self._message_id)
            if not reacted else self._on_remove_reaction(eh)
        )
        col.addWidget(self._reaction_bar)

        row.addLayout(col, 1)

        # Hover react button
        self._react_btn = QPushButton("😊", self)
        self._react_btn.setFixedSize(_REACT_BTN_SIZE, _REACT_BTN_SIZE)
        self._react_btn.setStyleSheet(
            "QPushButton { background: #333; color: #aaa; border: 1px solid #555; "
            "border-radius: 11px; font-size: 11px; }"
            "QPushButton:hover { background: #444; color: #fff; }"
        )
        self._react_btn.setToolTip("Add reaction")
        self._react_btn.hide()
        self._react_btn.clicked.connect(
            lambda: self.react_requested.emit(self._message_id)
        )

    def set_reaction_remove_callback(self, cb) -> None:
        """cb(message_id, emoji_hash) -- called when user clicks a reacted chip."""
        self._reaction_remove_cb = cb

    def _on_remove_reaction(self, emoji_hash: str) -> None:
        if self._reaction_remove_cb:
            self._reaction_remove_cb(self._message_id, emoji_hash)

    def refresh_reactions(self, storage: Storage, own_hash: str) -> None:
        """Rebuild the reaction chip bar from current DB state."""
        self._reaction_bar.refresh(self._message_id, storage, own_hash)

    def _react_btn_x(self) -> int:
        return self.width() - _REACT_BTN_SIZE - (_ROW_RIGHT_PAD - _REACT_BTN_SIZE) // 2

    def resizeEvent(self, event):
        self._react_btn.move(self._react_btn_x(), 4)
        super().resizeEvent(event)

    def enterEvent(self, event):
        self._ts_label.setStyleSheet(
            "color: #555; font-size: 10px; padding-right: 6px;"
        )
        self.setAutoFillBackground(True)
        p = self.palette()
        p.setColor(self.backgroundRole(), QColor(255, 255, 255, 12))
        self.setPalette(p)
        self._react_btn.move(self._react_btn_x(), 4)
        self._react_btn.raise_()
        self._react_btn.show()

    def leaveEvent(self, event):
        self._ts_label.setStyleSheet(
            "color: transparent; font-size: 10px; padding-right: 6px;"
        )
        self.setAutoFillBackground(False)
        self._react_btn.hide()


class ChannelView(QWidget):
    """Displays the message history for a single channel."""

    react_requested = pyqtSignal(str, str)    # (channel_hash_hex, message_id)
    reaction_remove_requested = pyqtSignal(str, str, str)  # (channel_hash_hex, message_id, emoji_hash)

    def __init__(self, channel_hash_hex: str, storage: Storage,
                 own_identity_hex: str, restore_to_id: str | None = None,
                 config=None, reaction_mgr: ReactionManager | None = None,
                 parent=None):
        super().__init__(parent)
        self._channel_hash = channel_hash_hex
        self._storage = storage
        self._own_hex = own_identity_hex
        self._reaction_mgr = reaction_mgr
        self._config = config
        self._displayed_ids: set[str] = set()
        # message_id -> QWidget (MessageBubble or MessageContinuation)
        self._bubble_map: dict[str, QWidget] = {}
        # identity_hash_hex -> QPixmap (raw, before circular clip)
        self._avatar_cache: dict[str, QPixmap] = {}
        # identity_hash_hex -> list of MessageBubble (header rows only, for avatar refresh)
        self._bubbles_by_sender: dict[str, list[MessageBubble]] = {}
        self._out_of_order_count = 0
        # Consumed on the first load_history() call; cleared afterwards so
        # subsequent reloads (out-of-order messages) always scroll to bottom.
        self._restore_to_id: str | None = restore_to_id
        # Grouping state — reset on load_history, updated per appended row
        self._last_sender: str | None = None
        self._last_ts: float = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._msg_layout = QVBoxLayout(self._container)
        self._msg_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._msg_layout.setSpacing(0)
        self._msg_layout.setContentsMargins(0, 8, 0, 8)
        self._msg_layout.addStretch()

        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll)

        self._out_of_order_bar = QLabel()
        self._out_of_order_bar.setStyleSheet(
            "background: #3a3000; color: #ffcc00; padding: 4px 8px; font-size: 11px;"
        )
        self._out_of_order_bar.hide()
        layout.addWidget(self._out_of_order_bar)

        self.load_history()

    def load_history(self):
        """Load all stored messages for this channel."""
        self._displayed_ids.clear()
        self._bubble_map.clear()
        self._avatar_cache.clear()
        self._bubbles_by_sender.clear()
        self._last_sender = None
        self._last_ts = 0.0
        # Clear existing bubbles (keep the stretch at index 0)
        while self._msg_layout.count() > 1:
            item = self._msg_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        rows = self._storage.get_messages(self._channel_hash, limit=_MESSAGE_HISTORY_LIMIT)
        for row in rows:
            self._append_bubble(row, scroll=False)

        # Consume the restore point: scroll to it on first open, then clear so
        # subsequent reloads (out-of-order arrivals) scroll to bottom instead.
        restore = self._restore_to_id
        self._restore_to_id = None
        if restore and restore in self._bubble_map:
            self._scroll_to_message(restore)
        else:
            self._scroll_to_bottom()

    def on_new_message(self, message_id: str):
        """Called when a new message arrives for this channel."""
        if message_id in self._displayed_ids:
            return

        rows = self._storage.get_messages(self._channel_hash, limit=_MESSAGE_HISTORY_LIMIT)
        # Find the new message
        for row in rows:
            if row["message_id"] == message_id:
                # Check if it's inserting into the middle (out of order)
                all_ids = [r["message_id"] for r in rows]
                pos = all_ids.index(message_id)
                is_late = (time.time() - row["timestamp"]) > LATE_THRESHOLD_SECS
                is_out_of_order = pos < len(all_ids) - 1

                if is_out_of_order and is_late:
                    self._out_of_order_count += 1
                    self._out_of_order_bar.setText(
                        f"{self._out_of_order_count} message(s) arrived out of order — "
                        "scroll up to see them"
                    )
                    self._out_of_order_bar.show()
                    # Re-render the full history to insert at correct position
                    self.load_history()
                else:
                    self._append_bubble(row, scroll=True)
                break

    def _get_avatar_pixmap(self, sender_hash: str) -> QPixmap | None:
        """Return a cached raw QPixmap for a sender, loading from DB on first access."""
        if sender_hash in self._avatar_cache:
            return self._avatar_cache[sender_hash]

        # Own avatar from config
        if sender_hash == self._own_hex and self._config is not None:
            avatar_bytes = self._config.avatar_bytes
            if avatar_bytes:
                pix = QPixmap()
                pix.loadFromData(avatar_bytes)
                if not pix.isNull():
                    self._avatar_cache[sender_hash] = pix
                    return pix
            return None

        row = self._storage.get_peer_avatar(sender_hash)
        if row and row.get("avatar_data"):
            pix = QPixmap()
            pix.loadFromData(bytes(row["avatar_data"]))
            if not pix.isNull():
                self._avatar_cache[sender_hash] = pix
                return pix
        return None

    def _append_bubble(self, row, scroll: bool = True):
        msg_id = row["message_id"]
        if msg_id in self._displayed_ids:
            return
        self._displayed_ids.add(msg_id)

        sender_hash = row["sender_hash"]
        sender_name = row["sender_name"] or sender_hash[:8]
        ts = row["timestamp"]
        received_at = row["received_at"]

        grouped = (
            sender_hash == self._last_sender
            and (ts - self._last_ts) < GROUP_WINDOW_SECS
        )
        self._last_sender = sender_hash
        self._last_ts = ts

        image_data = row["image_data"] if "image_data" in row.keys() else None
        if image_data is not None:
            image_data = bytes(image_data)

        if grouped:
            widget: QWidget = MessageContinuation(
                sender_hash=sender_hash,
                content=row["content"],
                timestamp=ts,
                received_at=received_at,
                message_id=msg_id,
                image_data=image_data,
                storage=self._storage,
                reaction_mgr=self._reaction_mgr,
            )
            widget.react_requested.connect(
                lambda mid, ch=self._channel_hash: self.react_requested.emit(ch, mid)
            )
            widget.set_reaction_remove_callback(
                lambda mid, eh, ch=self._channel_hash:
                    self.reaction_remove_requested.emit(ch, mid, eh)
            )
        else:
            avatar_pix = self._get_avatar_pixmap(sender_hash)
            bubble = MessageBubble(
                sender=sender_name,
                sender_hash=sender_hash,
                content=row["content"],
                timestamp=ts,
                received_at=received_at,
                message_id=msg_id,
                is_own=sender_hash == self._own_hex,
                avatar_pixmap=avatar_pix,
                image_data=image_data,
                storage=self._storage,
                reaction_mgr=self._reaction_mgr,
            )
            bubble.react_requested.connect(
                lambda mid, ch=self._channel_hash: self.react_requested.emit(ch, mid)
            )
            bubble.set_reaction_remove_callback(
                lambda mid, eh, ch=self._channel_hash:
                    self.reaction_remove_requested.emit(ch, mid, eh)
            )
            self._bubbles_by_sender.setdefault(sender_hash, []).append(bubble)
            widget = bubble

        # Pre-populate reactions for existing messages
        widget.refresh_reactions(self._storage, self._own_hex)

        self._bubble_map[msg_id] = widget
        self._msg_layout.insertWidget(self._msg_layout.count(), widget)

        if scroll:
            QTimer.singleShot(50, self._scroll_to_bottom)

    def on_reaction_updated(self, message_id: str) -> None:
        """Refresh the reaction bar for a single message when reactions change.

        Called from the main thread via a Qt signal when ReactionManager fires.
        """
        widget = self._bubble_map.get(message_id)
        if widget is not None:
            widget.refresh_reactions(self._storage, self._own_hex)

    def refresh_avatars(self, identity_hex: str) -> None:
        """Refresh avatar display for all visible bubbles from a given sender.

        Called from the main thread when a new avatar arrives (via _avatar_updated signal).
        """
        # Invalidate cache for this identity so we re-read from DB
        self._avatar_cache.pop(identity_hex, None)
        new_pix = self._get_avatar_pixmap(identity_hex)

        for bubble in self._bubbles_by_sender.get(identity_hex, []):
            # Retrieve sender_name from the bubble's header is not straightforward,
            # so look it up from storage instead.
            display_name = identity_hex[:8]
            try:
                row = self._storage.get_display_name_for_identity(identity_hex)
                if row:
                    display_name = row
            except Exception:
                pass
            bubble.update_avatar(new_pix, display_name)

    def _scroll_to_bottom(self):
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _scroll_to_message(self, message_id: str):
        """Scroll the view so message_id is visible (at the bottom of the viewport).
        Falls back to scrolling to the bottom if the message is not found.
        """
        bubble = self._bubble_map.get(message_id)
        if bubble is None:
            self._scroll_to_bottom()
            return
        QTimer.singleShot(50, lambda: self._scroll.ensureWidgetVisible(bubble))

    def clear_out_of_order_indicator(self):
        self._out_of_order_count = 0
        self._out_of_order_bar.hide()
