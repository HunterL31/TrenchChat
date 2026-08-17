"""
Passphrase dialogs for the TrenchChat lock system.

Three dialogs are provided:

* UnlockDialog  — shown at startup when a lock is active.
* SetPinDialog  — shown when the user sets a passphrase for the first time.
* ChangePinDialog — shown when the user changes or removes their passphrase.

The length policy lives in validate_passphrase() and applies only when
*setting* a passphrase.  Entering one is never length-checked: a lock created
before passphrases were allowed still holds a short numeric PIN, and rejecting
it here would lock its owner out of their own data.
"""

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QHBoxLayout, QWidget,
)

from trenchchat.core.lockbox import WrongPinError, unlock

# Maximum consecutive wrong guesses before a cooldown is imposed.
_MAX_ATTEMPTS = 5
# Cooldown duration in seconds after exceeding _MAX_ATTEMPTS.
_COOLDOWN_SECS = 30
# Accepted passphrase lengths when setting a new one.
_MIN_PASSPHRASE_LEN = 12
_MAX_PASSPHRASE_LEN = 256

_FIELD_WIDTH = 300


def validate_passphrase(text: str) -> str | None:
    """Return an error message for an unacceptable new passphrase, else None.

    Length is the only property that meaningfully resists offline guessing,
    so it is the only hard rule; composition requirements just push people
    toward predictable substitutions.
    """
    if len(text) < _MIN_PASSPHRASE_LEN:
        return f"Passphrase must be at least {_MIN_PASSPHRASE_LEN} characters."
    if len(text) > _MAX_PASSPHRASE_LEN:
        return f"Passphrase must be at most {_MAX_PASSPHRASE_LEN} characters."
    if len(set(text)) == 1:
        return "Passphrase must not be a single repeated character."
    return None


def _pin_field(placeholder: str = "Enter passphrase") -> QLineEdit:
    """Return a styled, masked QLineEdit."""
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setPlaceholderText(placeholder)
    edit.setMinimumWidth(_FIELD_WIDTH)
    return edit


def _reveal_checkbox(*fields: QLineEdit) -> QCheckBox:
    """Return a checkbox that unmasks the given passphrase fields."""
    box = QCheckBox("Show passphrase")

    def _toggle(checked: bool):
        mode = (QLineEdit.EchoMode.Normal if checked
                else QLineEdit.EchoMode.Password)
        for field in fields:
            field.setEchoMode(mode)

    box.toggled.connect(_toggle)
    return box


class UnlockDialog(QDialog):
    """Modal dialog asking the user to enter their passphrase to unlock.

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

        subtitle = QLabel("Enter your passphrase to unlock.")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        self._pin_edit = _pin_field()
        self._pin_edit.returnPressed.connect(self._on_unlock)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self._pin_edit)
        row.addStretch()
        layout.addLayout(row)

        reveal_row = QHBoxLayout()
        reveal_row.addStretch()
        reveal_row.addWidget(_reveal_checkbox(self._pin_edit))
        reveal_row.addStretch()
        layout.addLayout(reveal_row)

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

        # Deliberately not length-checked: an existing lock may hold a short
        # numeric PIN from before passphrases were allowed, and its owner must
        # still be able to enter it.
        pin = self._pin_edit.text().strip()
        if not pin:
            return

        try:
            self.raw_key = unlock(pin)
        except WrongPinError:
            self._attempts += 1
            self._pin_edit.clear()
            remaining = _MAX_ATTEMPTS - self._attempts
            if remaining > 0:
                self._error_label.setText(
                    f"Incorrect passphrase. {remaining} attempt(s) remaining."
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
    """Dialog for setting a new passphrase (enter + confirm).

    On acceptance the chosen passphrase is available via the ``pin`` attribute.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pin: str | None = None

        self.setWindowTitle("Set passphrase")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            f"Choose a passphrase of at least {_MIN_PASSPHRASE_LEN} characters "
            "to lock your identity and message database."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        caveat = QLabel(
            "This encrypts your data on this computer. Anyone who copies those "
            "files can try passphrases offline at their own pace — length is "
            "what stops them."
        )
        caveat.setWordWrap(True)
        caveat.setStyleSheet("color: #999;")
        layout.addWidget(caveat)

        self._pin1 = _pin_field("New passphrase")
        self._pin2 = _pin_field("Confirm passphrase")
        self._pin1.returnPressed.connect(self._pin2.setFocus)
        self._pin2.returnPressed.connect(self._on_accept)

        layout.addWidget(QLabel("New passphrase:"))
        layout.addWidget(self._pin1)
        layout.addWidget(QLabel("Confirm passphrase:"))
        layout.addWidget(self._pin2)
        layout.addWidget(_reveal_checkbox(self._pin1, self._pin2))

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
        error = validate_passphrase(p1)
        if error:
            self._error_label.setText(error)
            return
        if p1 != p2:
            self._error_label.setText("Passphrases do not match.")
            self._pin2.clear()
            self._pin2.setFocus()
            return
        self.pin = p1
        self.accept()


class ChangePinDialog(QDialog):
    """Dialog for changing or removing the current passphrase.

    After acceptance:
    * ``new_pin`` holds the new passphrase, or None if the user chose to
      remove passphrase protection entirely.
    * ``current_raw_key`` holds the derived key for the *current* (old)
      passphrase so the caller can use it for re-encryption.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.new_pin: str | None = None
        self.current_raw_key: bytes | None = None

        self.setWindowTitle("Change passphrase")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(
            "Enter your current passphrase, then set a new one."
        ))
        layout.addWidget(QLabel(
            "Leave the new fields empty to <b>remove</b> passphrase protection."
        ))

        self._current = _pin_field("Current passphrase")
        layout.addWidget(QLabel("Current passphrase:"))
        layout.addWidget(self._current)

        self._new1 = _pin_field("New passphrase (leave blank to remove)")
        self._new2 = _pin_field("Confirm new passphrase")
        layout.addWidget(QLabel("New passphrase:"))
        layout.addWidget(self._new1)
        layout.addWidget(QLabel("Confirm new passphrase:"))
        layout.addWidget(self._new2)
        layout.addWidget(_reveal_checkbox(self._current, self._new1, self._new2))

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

        # Validate the current passphrase first. Not length-checked -- an
        # existing lock may still hold a short numeric PIN.
        if not current_pin:
            self._error_label.setText("Enter your current passphrase.")
            return
        try:
            self.current_raw_key = unlock(current_pin)
        except WrongPinError:
            self._error_label.setText("Current passphrase is incorrect.")
            self._current.clear()
            self._current.setFocus()
            return

        # Validate the new passphrase (or removal path).
        if new1 == "" and new2 == "":
            # Removing protection — no new value needed.
            self.new_pin = None
            self.accept()
            return

        error = validate_passphrase(new1)
        if error:
            self._error_label.setText(f"{error} Or leave blank to remove.")
            return
        if new1 != new2:
            self._error_label.setText("New passphrases do not match.")
            self._new2.clear()
            self._new2.setFocus()
            return

        self.new_pin = new1
        self.accept()
