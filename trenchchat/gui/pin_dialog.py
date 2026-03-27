"""
PIN dialogs for the TrenchChat lock system.

Three dialogs are provided:

* UnlockDialog  — shown at startup when a PIN lock is active.
* SetPinDialog  — shown when the user sets a PIN for the first time.
* ChangePinDialog — shown when the user changes or removes their PIN.
"""

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QHBoxLayout, QWidget,
)

from trenchchat.core.lockbox import WrongPinError, unlock

# Maximum consecutive wrong guesses before a cooldown is imposed.
_MAX_ATTEMPTS = 5
# Cooldown duration in seconds after exceeding _MAX_ATTEMPTS.
_COOLDOWN_SECS = 30
# Minimum and maximum accepted PIN lengths.
_PIN_MIN_LEN = 4
_PIN_MAX_LEN = 8


def _pin_field(placeholder: str = "Enter PIN") -> QLineEdit:
    """Return a styled, numeric-only, masked QLineEdit."""
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setPlaceholderText(placeholder)
    edit.setMaxLength(_PIN_MAX_LEN)
    edit.setValidator(QIntValidator(0, 99_999_999))
    edit.setFixedWidth(180)
    return edit


class UnlockDialog(QDialog):
    """Modal dialog asking the user to enter their PIN to unlock TrenchChat.

    On success the derived raw key is available via the ``raw_key`` attribute.
    The dialog enforces a 5-attempt limit before imposing a 30-second cooldown.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.raw_key: bytes | None = None
        self._attempts = 0
        self._locked_until: float = 0.0

        self.setWindowTitle("TrenchChat — Unlock")
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("<b>TrenchChat is locked</b>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Enter your PIN to unlock.")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        self._pin_edit = _pin_field()
        self._pin_edit.returnPressed.connect(self._on_unlock)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self._pin_edit)
        row.addStretch()
        layout.addLayout(row)

        self._error_label = QLabel("")
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.setStyleSheet("color: #e55;")
        layout.addWidget(self._error_label)

        self._unlock_btn = QPushButton("Unlock")
        self._unlock_btn.setDefault(True)
        self._unlock_btn.clicked.connect(self._on_unlock)

        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(quit_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._unlock_btn)
        layout.addLayout(btn_row)

        # Timer used to count down the cooldown and re-enable the field.
        self._cooldown_timer = QTimer(self)
        self._cooldown_timer.timeout.connect(self._tick_cooldown)

    def _on_unlock(self):
        now = time.monotonic()
        if now < self._locked_until:
            return

        pin = self._pin_edit.text().strip()
        if len(pin) < _PIN_MIN_LEN:
            self._error_label.setText(
                f"PIN must be {_PIN_MIN_LEN}–{_PIN_MAX_LEN} digits."
            )
            return

        try:
            self.raw_key = unlock(pin)
        except WrongPinError:
            self._attempts += 1
            self._pin_edit.clear()
            remaining = _MAX_ATTEMPTS - self._attempts
            if remaining > 0:
                self._error_label.setText(
                    f"Incorrect PIN. {remaining} attempt(s) remaining."
                )
            else:
                self._attempts = 0
                self._locked_until = time.monotonic() + _COOLDOWN_SECS
                self._set_cooldown_state(True)
                self._cooldown_timer.start(1000)
            return

        self.accept()

    def _set_cooldown_state(self, locked: bool):
        self._pin_edit.setEnabled(not locked)
        self._unlock_btn.setEnabled(not locked)

    def _tick_cooldown(self):
        remaining = max(0, self._locked_until - time.monotonic())
        if remaining <= 0:
            self._cooldown_timer.stop()
            self._set_cooldown_state(False)
            self._error_label.setText("")
            self._pin_edit.setFocus()
        else:
            secs = int(remaining) + 1
            self._error_label.setText(f"Too many attempts. Wait {secs}s.")


class SetPinDialog(QDialog):
    """Dialog for setting a new PIN (enter + confirm).

    On acceptance the chosen PIN is available via the ``pin`` attribute.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pin: str | None = None

        self.setWindowTitle("Set PIN")
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(
            f"Choose a {_PIN_MIN_LEN}–{_PIN_MAX_LEN} digit numeric PIN to lock "
            "your identity and message database."
        ))

        self._pin1 = _pin_field("New PIN")
        self._pin2 = _pin_field("Confirm PIN")
        self._pin1.returnPressed.connect(self._pin2.setFocus)
        self._pin2.returnPressed.connect(self._on_accept)

        layout.addWidget(QLabel("New PIN:"))
        layout.addWidget(self._pin1)
        layout.addWidget(QLabel("Confirm PIN:"))
        layout.addWidget(self._pin2)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: #e55;")
        layout.addWidget(self._error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        p1 = self._pin1.text().strip()
        p2 = self._pin2.text().strip()
        if len(p1) < _PIN_MIN_LEN:
            self._error_label.setText(
                f"PIN must be at least {_PIN_MIN_LEN} digits."
            )
            return
        if p1 != p2:
            self._error_label.setText("PINs do not match.")
            self._pin2.clear()
            self._pin2.setFocus()
            return
        self.pin = p1
        self.accept()


class ChangePinDialog(QDialog):
    """Dialog for changing or removing the current PIN.

    After acceptance:
    * ``new_pin`` holds the new PIN string, or None if the user chose to
      remove the PIN entirely.
    * ``raw_key`` holds the derived key for the *current* (old) PIN so the
      caller can verify it and use it for re-encryption.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.new_pin: str | None = None
        self.current_raw_key: bytes | None = None

        self.setWindowTitle("Change PIN")
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Enter your current PIN, then set a new one."))
        layout.addWidget(QLabel(
            "Leave the new PIN fields empty to <b>remove</b> PIN protection."
        ))

        self._current = _pin_field("Current PIN")
        layout.addWidget(QLabel("Current PIN:"))
        layout.addWidget(self._current)

        self._new1 = _pin_field("New PIN (leave blank to remove)")
        self._new2 = _pin_field("Confirm new PIN")
        self._new1.setValidator(None)  # allow blank for removal
        self._new2.setValidator(None)
        layout.addWidget(QLabel("New PIN:"))
        layout.addWidget(self._new1)
        layout.addWidget(QLabel("Confirm new PIN:"))
        layout.addWidget(self._new2)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: #e55;")
        layout.addWidget(self._error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        current_pin = self._current.text().strip()
        new1 = self._new1.text().strip()
        new2 = self._new2.text().strip()

        # Validate the current PIN first.
        if len(current_pin) < _PIN_MIN_LEN:
            self._error_label.setText("Enter your current PIN.")
            return
        try:
            self.current_raw_key = unlock(current_pin)
        except WrongPinError:
            self._error_label.setText("Current PIN is incorrect.")
            self._current.clear()
            self._current.setFocus()
            return

        # Validate the new PIN (or removal path).
        if new1 == "" and new2 == "":
            # Removing PIN — no new value needed.
            self.new_pin = None
            self.accept()
            return

        if len(new1) < _PIN_MIN_LEN:
            self._error_label.setText(
                f"New PIN must be at least {_PIN_MIN_LEN} digits (or leave blank to remove)."
            )
            return
        if not new1.isdigit():
            self._error_label.setText("New PIN must contain digits only.")
            return
        if len(new1) > _PIN_MAX_LEN:
            self._error_label.setText(f"New PIN must be at most {_PIN_MAX_LEN} digits.")
            return
        if new1 != new2:
            self._error_label.setText("New PINs do not match.")
            self._new2.clear()
            self._new2.setFocus()
            return

        self.new_pin = new1
        self.accept()
