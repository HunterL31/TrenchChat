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

import datetime
import hashlib
import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPixmap, QPainter, QPainterPath

from trenchchat.core.storage import Storage

# Messages received more than this many seconds after their timestamp are "late"
LATE_THRESHOLD_SECS = 30.0

_MESSAGE_HISTORY_LIMIT = 500
_AVATAR_SIZE = 36              # avatar circle diameter in pixels
_ROW_LEFT_PAD = 12             # padding left of avatar
_ROW_RIGHT_PAD = 16            # padding right edge
_ROW_V_PAD = 4                 # vertical padding for header rows
_ROW_V_PAD_CONT = 1            # vertical padding for continuation rows
_AVATAR_TEXT_GAP = 10          # gap between avatar column and text column
# Seconds within which consecutive messages from the same sender are grouped
GROUP_WINDOW_SECS = 300


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

    def __init__(self, sender: str, sender_hash: str, content: str, timestamp: float,
                 received_at: float, is_own: bool = False,
                 avatar_pixmap: QPixmap | None = None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._sender_hash = sender_hash

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

        body = QLabel(content)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet("color: #dcddde; font-size: 13px;")
        col.addWidget(body)

        if received_at - timestamp > LATE_THRESHOLD_SECS:
            late = QLabel("⟳ received late")
            late.setStyleSheet("color: #666; font-size: 10px; font-style: italic;")
            col.addWidget(late)

        row.addLayout(col, 1)

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

    def enterEvent(self, event):
        self.setAutoFillBackground(True)
        p = self.palette()
        p.setColor(self.backgroundRole(), QColor(255, 255, 255, 12))
        self.setPalette(p)

    def leaveEvent(self, event):
        self.setAutoFillBackground(False)


class MessageContinuation(QWidget):
    """Grouped follow-on message row with no avatar or name header.

    Indented to align with the text column of the preceding MessageBubble.
    On hover, a faint HH:MM timestamp appears to the left of the message text
    and the row gets a subtle background highlight.
    """

    # Width of the "gutter" that replaces the avatar column so text aligns.
    # Must equal _ROW_LEFT_PAD + _AVATAR_SIZE + _AVATAR_TEXT_GAP.
    _GUTTER = _ROW_LEFT_PAD + _AVATAR_SIZE + _AVATAR_TEXT_GAP

    def __init__(self, sender_hash: str, content: str, timestamp: float,
                 received_at: float, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

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

        self._body = QLabel(content)
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body.setStyleSheet("color: #dcddde; font-size: 13px;")
        col.addWidget(self._body)

        if received_at - timestamp > LATE_THRESHOLD_SECS:
            late = QLabel("⟳ received late")
            late.setStyleSheet("color: #666; font-size: 10px; font-style: italic;")
            col.addWidget(late)

        row.addLayout(col, 1)

    def enterEvent(self, event):
        self._ts_label.setStyleSheet(
            "color: #555; font-size: 10px; padding-right: 6px;"
        )
        self.setAutoFillBackground(True)
        p = self.palette()
        p.setColor(self.backgroundRole(), QColor(255, 255, 255, 12))
        self.setPalette(p)

    def leaveEvent(self, event):
        self._ts_label.setStyleSheet(
            "color: transparent; font-size: 10px; padding-right: 6px;"
        )
        self.setAutoFillBackground(False)


class ChannelView(QWidget):
    """Displays the message history for a single channel."""

    def __init__(self, channel_hash_hex: str, storage: Storage,
                 own_identity_hex: str, restore_to_id: str | None = None,
                 config=None, parent=None):
        super().__init__(parent)
        self._channel_hash = channel_hash_hex
        self._storage = storage
        self._own_hex = own_identity_hex
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

        if grouped:
            widget: QWidget = MessageContinuation(
                sender_hash=sender_hash,
                content=row["content"],
                timestamp=ts,
                received_at=received_at,
            )
        else:
            avatar_pix = self._get_avatar_pixmap(sender_hash)
            bubble = MessageBubble(
                sender=sender_name,
                sender_hash=sender_hash,
                content=row["content"],
                timestamp=ts,
                received_at=received_at,
                is_own=sender_hash == self._own_hex,
                avatar_pixmap=avatar_pix,
            )
            self._bubbles_by_sender.setdefault(sender_hash, []).append(bubble)
            widget = bubble

        self._bubble_map[msg_id] = widget
        self._msg_layout.insertWidget(self._msg_layout.count(), widget)

        if scroll:
            QTimer.singleShot(50, self._scroll_to_bottom)

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
