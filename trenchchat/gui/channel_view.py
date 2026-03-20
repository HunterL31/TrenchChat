"""
Per-channel message display widget.

Shows messages sorted by timestamp with causal tiebreaking via last_seen_id.
Late-arriving messages are flagged visually.
"""

import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QScrollArea, QLabel, QFrame, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QPalette

from trenchchat.core.storage import Storage

# Messages received more than this many seconds after their timestamp are "late"
LATE_THRESHOLD_SECS = 30.0


def _format_ts(ts: float) -> str:
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M")


class MessageBubble(QFrame):
    def __init__(self, sender: str, sender_hash: str, content: str, timestamp: float,
                 received_at: float, is_own: bool = False, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        # Header: sender + truncated hash badge + time
        hash_badge = f"<span style='color:#666;font-size:10px'>[{sender_hash[:8]}]</span>"
        header = QLabel(f"<b>{sender}</b> {hash_badge}  <span style='color:#888;font-size:11px'>"
                        f"{_format_ts(timestamp)}</span>")
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        # Content
        body = QLabel(content)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body)

        # Late indicator
        if received_at - timestamp > LATE_THRESHOLD_SECS:
            late_label = QLabel("⟳ received late")
            late_label.setStyleSheet("color: #888; font-size: 10px; font-style: italic;")
            layout.addWidget(late_label)

        if is_own:
            self.setStyleSheet("background: #1e3a5f; border-radius: 6px; margin: 2px 40px 2px 8px;")
        else:
            self.setStyleSheet("background: #2a2a2a; border-radius: 6px; margin: 2px 8px 2px 40px;")


class ChannelView(QWidget):
    """Displays the message history for a single channel."""

    request_scroll_indicator = pyqtSignal(int)  # number of out-of-order messages

    def __init__(self, channel_hash_hex: str, storage: Storage,
                 own_identity_hex: str, parent=None):
        super().__init__(parent)
        self._channel_hash = channel_hash_hex
        self._storage = storage
        self._own_hex = own_identity_hex
        self._displayed_ids: set[str] = set()
        self._out_of_order_count = 0

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
        # Clear existing bubbles (keep the stretch at index 0)
        while self._msg_layout.count() > 1:
            item = self._msg_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        rows = self._storage.get_messages(self._channel_hash, limit=500)
        for row in rows:
            self._append_bubble(row, scroll=False)

        self._scroll_to_bottom()

    def on_new_message(self, message_id: str):
        """Called when a new message arrives for this channel."""
        if message_id in self._displayed_ids:
            return

        rows = self._storage.get_messages(self._channel_hash, limit=500)
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

    def _append_bubble(self, row, scroll: bool = True):
        msg_id = row["message_id"]
        if msg_id in self._displayed_ids:
            return
        self._displayed_ids.add(msg_id)

        bubble = MessageBubble(
            sender=row["sender_name"] or row["sender_hash"][:8],
            sender_hash=row["sender_hash"],
            content=row["content"],
            timestamp=row["timestamp"],
            received_at=row["received_at"],
            is_own=row["sender_hash"] == self._own_hex,
        )
        # Insert before the stretch (index 0)
        self._msg_layout.insertWidget(self._msg_layout.count(), bubble)

        if scroll:
            QTimer.singleShot(50, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_out_of_order_indicator(self):
        self._out_of_order_count = 0
        self._out_of_order_bar.hide()
