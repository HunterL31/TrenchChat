"""
Voice channel panel widget.

Replaces the compose area when a voice channel is selected.  Shows the
participant list with speaking indicators and provides mute, deafen, and
PTT/VAD controls.

Layout:
    ┌──────────────────────────────────────────┐
    │  Voice: #general-voice  [Relay: ...]     │
    ├──────────────────────────────────────────┤
    │  ● Alice (speaking)                      │
    │  ○ Bob (muted)                           │
    │  ○ Charlie                               │
    ├──────────────────────────────────────────┤
    │  [Mic] [Deafen]  Mode: [PTT ▼]           │
    │  [Join Voice] / [Leave Voice]            │
    └──────────────────────────────────────────┘
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QComboBox, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QFont, QColor

from trenchchat.core.voice import VoiceManager
from trenchchat.core.storage import Storage
from trenchchat.core.permissions import SPEAK, MANAGE_RELAY


class _ParticipantItem(QListWidgetItem):
    """List widget item for a voice channel participant."""

    def __init__(self, identity_hex: str, display_name: str,
                 is_speaking: bool = False, is_muted: bool = False):
        super().__init__()
        self.identity_hex = identity_hex
        self.update_state(display_name, is_speaking, is_muted)

    def update_state(self, display_name: str, is_speaking: bool,
                     is_muted: bool) -> None:
        """Refresh label text and colour based on current state."""
        indicator = "●" if is_speaking else "○"
        suffix = " (muted)" if is_muted else ""
        self.setText(f"  {indicator}  {display_name}{suffix}")
        if is_speaking:
            self.setForeground(QColor("#4caf50"))
        elif is_muted:
            self.setForeground(QColor("#888888"))
        else:
            self.setForeground(QColor("#e0e0e0"))


class VoicePanel(QWidget):
    """Voice channel control panel.

    All mutations go through VoiceManager; this widget only reads Storage for
    display purposes.

    Signals:
        join_requested(channel_hash_hex)  -- user clicked Join Voice
        leave_requested(channel_hash_hex) -- user clicked Leave Voice
        relay_settings_requested(channel_hash_hex) -- relay settings clicked
    """

    join_requested = pyqtSignal(str)
    leave_requested = pyqtSignal(str)
    relay_settings_requested = pyqtSignal(str)

    # Emitted internally from VoiceManager callbacks (background thread) to
    # drive Qt UI updates safely on the main thread.
    _participant_changed = pyqtSignal(str)
    _speaking_changed = pyqtSignal(str, str)
    _voice_state_received = pyqtSignal(str, str, list)

    def __init__(self, voice_mgr: VoiceManager, storage: Storage,
                 identity_hex: str, parent=None):
        super().__init__(parent)
        self._voice_mgr = voice_mgr
        self._storage = storage
        self._identity_hex = identity_hex
        self._channel_hash_hex: str | None = None

        # Known participant state: identity_hex -> dict
        self._participants: dict[str, dict] = {}
        # Last received voice dest (from MT_VOICE_STATE)
        self._voice_dest_hex: str | None = None

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(4)

        # --- Header ---
        header_row = QHBoxLayout()
        self._title_label = QLabel("Voice Channel")
        title_font = QFont()
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        header_row.addWidget(self._title_label)
        header_row.addStretch()
        self._relay_label = QLabel()
        self._relay_label.setStyleSheet("color: #888; font-size: 11px;")
        header_row.addWidget(self._relay_label)
        root.addLayout(header_row)

        # --- Separator ---
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(line)

        # --- Participant list ---
        self._participant_list = QListWidget()
        self._participant_list.setMinimumHeight(80)
        self._participant_list.setMaximumHeight(160)
        self._participant_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._participant_list.setStyleSheet(
            "QListWidget { background: #1e1e1e; border: none; }"
        )
        root.addWidget(self._participant_list)

        # --- Controls row ---
        controls = QHBoxLayout()

        self._mute_btn = QPushButton("Mic: On")
        self._mute_btn.setCheckable(True)
        self._mute_btn.setToolTip("Toggle microphone mute")
        self._mute_btn.clicked.connect(self._on_mute_toggled)
        controls.addWidget(self._mute_btn)

        self._deafen_btn = QPushButton("Deafen: Off")
        self._deafen_btn.setCheckable(True)
        self._deafen_btn.setToolTip("Toggle speaker output")
        self._deafen_btn.clicked.connect(self._on_deafen_toggled)
        controls.addWidget(self._deafen_btn)

        controls.addStretch()

        mode_label = QLabel("Mode:")
        controls.addWidget(mode_label)
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["PTT", "Voice Activity"])
        self._mode_combo.setToolTip("Select microphone activation mode")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        controls.addWidget(self._mode_combo)

        root.addLayout(controls)

        # --- PTT button ---
        self._ptt_btn = QPushButton("Hold to Talk")
        self._ptt_btn.setToolTip("Push-to-talk: hold while speaking")
        self._ptt_btn.pressed.connect(self._on_ptt_pressed)
        self._ptt_btn.released.connect(self._on_ptt_released)
        root.addWidget(self._ptt_btn)

        # --- Join / Leave row ---
        join_row = QHBoxLayout()

        self._join_btn = QPushButton("Join Voice")
        self._join_btn.clicked.connect(self._on_join_clicked)
        join_row.addWidget(self._join_btn)

        self._leave_btn = QPushButton("Leave Voice")
        self._leave_btn.setEnabled(False)
        self._leave_btn.clicked.connect(self._on_leave_clicked)
        join_row.addWidget(self._leave_btn)

        root.addLayout(join_row)

        self._update_controls()

    def _connect_signals(self) -> None:
        self._participant_changed.connect(self._refresh_participant_list)
        self._speaking_changed.connect(self._on_speaking_changed_slot)
        self._voice_state_received.connect(self._on_voice_state_slot)

        # Store bound callback references so we can remove them on destruction.
        self._cb_participant = self._participant_changed.emit
        self._cb_speaking = self._speaking_changed.emit
        self._cb_voice_state = (
            lambda ch, dest, parts: self._voice_state_received.emit(ch, dest or "", parts)
        )

        self._voice_mgr.add_participant_changed_callback(self._cb_participant)
        self._voice_mgr.add_speaking_changed_callback(self._cb_speaking)
        self._voice_mgr.add_voice_state_callback(self._cb_voice_state)

    def _disconnect_signals(self) -> None:
        """Unregister VoiceManager callbacks to prevent use-after-free crashes."""
        self._voice_mgr.remove_participant_changed_callback(self._cb_participant)
        self._voice_mgr.remove_speaking_changed_callback(self._cb_speaking)
        self._voice_mgr.remove_voice_state_callback(self._cb_voice_state)

    def closeEvent(self, event) -> None:
        """Disconnect callbacks before the Qt object is destroyed."""
        self._disconnect_signals()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_channel(self, channel_hash_hex: str) -> None:
        """Switch the panel to display a different voice channel."""
        self._channel_hash_hex = channel_hash_hex
        self._participants = {}
        self._voice_dest_hex = None

        channel = self._storage.get_channel(channel_hash_hex)
        name = channel["name"] if channel else channel_hash_hex[:8]
        self._title_label.setText(f"Voice: #{name}")

        relay_dest = channel["relay_dest_hash"] if channel and "relay_dest_hash" in channel.keys() else None
        if relay_dest:
            self._relay_label.setText(f"Relay: {relay_dest[:8]}…")
        else:
            self._relay_label.setText("")

        # Check SPEAK permission to decide whether to show controls.
        can_speak = self._storage.has_permission(
            channel_hash_hex, self._identity_hex, SPEAK
        )

        self._join_btn.setEnabled(can_speak)
        self._mute_btn.setEnabled(False)
        self._deafen_btn.setEnabled(False)
        self._ptt_btn.setEnabled(False)

        # Show relay settings option for channel owners with MANAGE_RELAY.
        can_manage_relay = self._storage.has_permission(
            channel_hash_hex, self._identity_hex, MANAGE_RELAY
        )
        # (The relay settings button is in the main window context menu; we
        # just need to know if we should enable those actions.)
        self._can_manage_relay = can_manage_relay

        self._refresh_participant_list(channel_hash_hex)
        self._update_controls()

    def set_voice_mode(self, mode: str) -> None:
        """Set the displayed mode selector without triggering the callback."""
        idx = 0 if mode == "ptt" else 1
        self._mode_combo.blockSignals(True)
        self._mode_combo.setCurrentIndex(idx)
        self._mode_combo.blockSignals(False)
        self._update_ptt_visibility()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(str)
    def _refresh_participant_list(self, channel_hash_hex: str) -> None:
        if channel_hash_hex != self._channel_hash_hex:
            return

        in_voice = self._voice_mgr.is_in_voice(channel_hash_hex)
        self._join_btn.setEnabled(not in_voice)
        self._leave_btn.setEnabled(in_voice)
        self._mute_btn.setEnabled(in_voice)
        self._deafen_btn.setEnabled(in_voice)
        self._update_controls()

        # Refresh participant list from VoiceManager.
        if self._voice_mgr.is_hosting(channel_hash_hex):
            participants = self._voice_mgr.get_participants(channel_hash_hex)
            self._participants = {p["identity_hex"]: p for p in participants}

        self._participant_list.clear()
        for p in self._participants.values():
            item = _ParticipantItem(
                p["identity_hex"],
                p.get("display_name", p["identity_hex"][:8]),
                is_speaking=p.get("is_speaking", False),
                is_muted=p.get("is_muted", False),
            )
            self._participant_list.addItem(item)

        # Show placeholder if no participants.
        if self._participant_list.count() == 0:
            placeholder = QListWidgetItem("  No participants in voice")
            placeholder.setForeground(QColor("#555"))
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._participant_list.addItem(placeholder)

    @pyqtSlot(str, str)
    def _on_speaking_changed_slot(self, channel_hash_hex: str, identity_hex: str) -> None:
        if channel_hash_hex != self._channel_hash_hex:
            return
        # Update in our local participant state and refresh.
        if self._voice_mgr.is_hosting(channel_hash_hex):
            participants = self._voice_mgr.get_participants(channel_hash_hex)
            self._participants = {p["identity_hex"]: p for p in participants}
        self._refresh_participant_list(channel_hash_hex)

    @pyqtSlot(str, str, list)
    def _on_voice_state_slot(self, channel_hash_hex: str, voice_dest_hex: str,
                              participants: list) -> None:
        if channel_hash_hex != self._channel_hash_hex:
            return
        self._voice_dest_hex = voice_dest_hex or None
        # Merge participant list from remote voice state.
        for identity_hex in participants:
            if identity_hex not in self._participants:
                display_name = (
                    self._storage.get_display_name_for_identity(identity_hex)
                    or identity_hex[:8]
                )
                self._participants[identity_hex] = {
                    "identity_hex": identity_hex,
                    "display_name": display_name,
                    "is_speaking": False,
                    "is_muted": False,
                }
        # Remove participants no longer listed.
        for gone in set(self._participants) - set(participants):
            self._participants.pop(gone, None)
        self._refresh_participant_list(channel_hash_hex)

    def _on_mute_toggled(self, checked: bool) -> None:
        """GUI guard: re-check permission before mutating mic state."""
        if self._channel_hash_hex is None:
            return
        if not self._storage.has_permission(
            self._channel_hash_hex, self._identity_hex, SPEAK
        ):
            self._mute_btn.setChecked(False)
            return
        self._voice_mgr.set_muted(self._channel_hash_hex, checked)
        self._mute_btn.setText("Mic: Off" if checked else "Mic: On")

    def _on_deafen_toggled(self, checked: bool) -> None:
        if self._channel_hash_hex is None:
            return
        self._voice_mgr.set_deafened(self._channel_hash_hex, checked)
        self._deafen_btn.setText("Deafen: On" if checked else "Deafen: Off")

    def _on_ptt_pressed(self) -> None:
        """GUI guard: re-check permission before activating PTT."""
        if self._channel_hash_hex is None:
            return
        if not self._storage.has_permission(
            self._channel_hash_hex, self._identity_hex, SPEAK
        ):
            return
        self._voice_mgr.set_ptt_active(self._channel_hash_hex, True)
        self._ptt_btn.setStyleSheet("background-color: #4caf50; color: black;")

    def _on_ptt_released(self) -> None:
        if self._channel_hash_hex is None:
            return
        self._voice_mgr.set_ptt_active(self._channel_hash_hex, False)
        self._ptt_btn.setStyleSheet("")

    def _on_mode_changed(self, index: int) -> None:
        if self._channel_hash_hex is None:
            return
        mode = "ptt" if index == 0 else "vad"
        self._voice_mgr.set_mic_mode(self._channel_hash_hex, mode)
        self._update_ptt_visibility()

    def _on_join_clicked(self) -> None:
        """GUI outbound guard: re-check SPEAK before emitting join_requested."""
        if self._channel_hash_hex is None:
            return
        if not self._storage.has_permission(
            self._channel_hash_hex, self._identity_hex, SPEAK
        ):
            return
        self.join_requested.emit(self._channel_hash_hex)

    def _on_leave_clicked(self) -> None:
        if self._channel_hash_hex is None:
            return
        self.leave_requested.emit(self._channel_hash_hex)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _update_controls(self) -> None:
        """Show/hide PTT button based on current mode and session state."""
        self._update_ptt_visibility()
        in_voice = (
            self._channel_hash_hex is not None
            and self._voice_mgr.is_in_voice(self._channel_hash_hex)
        )
        self._mute_btn.setEnabled(in_voice)
        self._deafen_btn.setEnabled(in_voice)
        mode = self._voice_mgr.get_mic_mode(self._channel_hash_hex) if self._channel_hash_hex else "ptt"
        self._ptt_btn.setEnabled(in_voice and mode == "ptt")
        self._join_btn.setEnabled(
            not in_voice and self._channel_hash_hex is not None
        )
        self._leave_btn.setEnabled(in_voice)

    def _update_ptt_visibility(self) -> None:
        mode = self._mode_combo.currentIndex()
        self._ptt_btn.setVisible(mode == 0)  # 0 = PTT
