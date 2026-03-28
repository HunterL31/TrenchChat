"""
Per-channel message display widget.

Shows messages sorted by timestamp with causal tiebreaking via last_seen_id.
Late-arriving messages are flagged visually.
"""

import datetime
import hashlib
import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QFrame, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QPalette, QPixmap, QPainter, QPainterPath

from trenchchat.core.storage import Storage

# Messages received more than this many seconds after their timestamp are "late"
LATE_THRESHOLD_SECS = 30.0

_MESSAGE_HISTORY_LIMIT = 500
_AVATAR_DISPLAY_SIZE = 32   # px, displayed in message bubbles


def _format_ts(ts: float) -> str:
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M")


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
    """Return a colored circle with the first letter of the display name."""
    # Derive a stable color from the identity hash
    digest = hashlib.md5(identity_hex.encode()).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    color = QColor.fromHsv(hue, 160, 180)

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


class _AvatarWidget(QWidget):
    """Fixed-size widget that paints a circular avatar without leaking stylesheet."""

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
    """A single message row showing avatar, sender name, time, and content.

    Uses QWidget (not QFrame) so that the bubble background stylesheet does not
    cascade into child labels and corrupt their text rendering.
    """

    def __init__(self, sender: str, sender_hash: str, content: str, timestamp: float,
                 received_at: float, is_own: bool = False,
                 avatar_pixmap: QPixmap | None = None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._sender_hash = sender_hash
        self._is_own = is_own

        # Outer row: avatar + text block, with spacer on the appropriate side
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(8)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Avatar widget — uses custom paint so stylesheet doesn't affect it
        self._avatar_widget = _AvatarWidget(_AVATAR_DISPLAY_SIZE, self)
        self._set_avatar_pixmap(avatar_pixmap, sender, sender_hash)

        # Text block
        inner = QVBoxLayout()
        inner.setSpacing(2)
        inner.setContentsMargins(0, 0, 0, 0)

        hash_badge = f"<span style='color:#888;font-size:10px'>[{sender_hash[:8]}]</span>"
        header = QLabel(
            f"<b>{sender}</b> {hash_badge}"
            f"&nbsp;&nbsp;<span style='color:#999;font-size:10px'>{_format_ts(timestamp)}</span>"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        inner.addWidget(header)

        body = QLabel(content)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        inner.addWidget(body)

        if received_at - timestamp > LATE_THRESHOLD_SECS:
            late_label = QLabel("⟳ received late")
            late_label.setStyleSheet("color: #888; font-size: 10px; font-style: italic;")
            inner.addWidget(late_label)

        if is_own:
            outer.addStretch()
            outer.addLayout(inner)
            outer.addWidget(self._avatar_widget)
            self.setStyleSheet(
                "MessageBubble { background: #1e3a5f; border-radius: 6px;"
                " margin: 2px 8px 2px 56px; }"
            )
        else:
            outer.addWidget(self._avatar_widget)
            outer.addLayout(inner)
            outer.addStretch()
            self.setStyleSheet(
                "MessageBubble { background: #2a2a2a; border-radius: 6px;"
                " margin: 2px 56px 2px 8px; }"
            )

    def update_avatar(self, avatar_pixmap: QPixmap | None,
                      display_name: str) -> None:
        """Replace the avatar image (called when a new avatar arrives for this sender)."""
        self._set_avatar_pixmap(avatar_pixmap, display_name, self._sender_hash)

    def _set_avatar_pixmap(self, avatar_pixmap: QPixmap | None,
                           display_name: str, sender_hash: str) -> None:
        if avatar_pixmap and not avatar_pixmap.isNull():
            pix = _make_circular_pixmap(avatar_pixmap, _AVATAR_DISPLAY_SIZE)
        else:
            pix = _make_placeholder_pixmap(sender_hash, display_name, _AVATAR_DISPLAY_SIZE)
        self._avatar_widget.set_pixmap(pix)


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
        self._bubble_map: dict[str, MessageBubble] = {}
        # identity_hash_hex -> QPixmap (raw, before circular clip)
        self._avatar_cache: dict[str, QPixmap] = {}
        # identity_hash_hex -> list of MessageBubble (for batch refresh)
        self._bubbles_by_sender: dict[str, list[MessageBubble]] = {}
        self._out_of_order_count = 0
        # Consumed on the first load_history() call; cleared afterwards so
        # subsequent reloads (out-of-order messages) always scroll to bottom.
        self._restore_to_id: str | None = restore_to_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._msg_layout = QVBoxLayout(self._container)
        self._msg_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._msg_layout.setSpacing(2)
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
        avatar_pix = self._get_avatar_pixmap(sender_hash)

        bubble = MessageBubble(
            sender=sender_name,
            sender_hash=sender_hash,
            content=row["content"],
            timestamp=row["timestamp"],
            received_at=row["received_at"],
            is_own=sender_hash == self._own_hex,
            avatar_pixmap=avatar_pix,
        )
        self._bubble_map[msg_id] = bubble
        self._bubbles_by_sender.setdefault(sender_hash, []).append(bubble)

        # Insert before the stretch (index 0)
        self._msg_layout.insertWidget(self._msg_layout.count(), bubble)

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
