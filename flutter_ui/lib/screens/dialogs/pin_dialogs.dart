// PIN dialogs: UnlockDialog, SetPinDialog, ChangePinDialog. The lockbox
// has no API surface yet (locked-start vs out-of-band key is an open
// design question), so verification is injected via [verifyPin] and
// nothing is persisted; the dialogs and their limits are the port.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../theme/glow.dart';
import '../../theme/section_theme.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';

const int pinMinLen = 4;
const int pinMaxLen = 8;
const int maxUnlockAttempts = 5;
const int unlockCooldownSecs = 30;

/// Change-PIN outcome: [newPin] null means the user chose to remove the PIN.
class PinChange {
  const PinChange({required this.newPin});
  final String? newPin;
}

/// Masked, numeric-only PIN field matching pin_dialog.py's _pin_field.
class _PinField extends StatelessWidget {
  const _PinField({
    required this.controller,
    required this.hint,
    this.autofocus = false,
    this.enabled = true,
    this.onSubmitted,
  });

  final TextEditingController controller;
  final String hint;
  final bool autofocus;
  final bool enabled;
  final ValueChanged<String>? onSubmitted;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      decoration: BoxDecoration(
        color: tc.bgInset,
        border: Border.all(color: tc.borderDefault),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      child: TextField(
        controller: controller,
        autofocus: autofocus,
        enabled: enabled,
        obscureText: true,
        maxLength: pinMaxLen,
        inputFormatters: [FilteringTextInputFormatter.digitsOnly],
        onSubmitted: onSubmitted,
        style: TextStyle(
          fontSize: TCType.textBodyMd,
          color: tc.textPrimary,
          letterSpacing: TCType.letterSpacingFor(TCType.textBodyMd, TCType.trackingWider),
        ),
        decoration: InputDecoration(
          isDense: true,
          counterText: '',
          border: InputBorder.none,
          hintText: hint,
          hintStyle: TextStyle(fontSize: TCType.textBodyMd, color: tc.textTertiary),
        ),
      ),
    );
  }
}

Widget _fieldLabel(BuildContext context, String label) => Text(
      label,
      style: TextStyle(
        fontSize: TCType.textCaption,
        color: SectionTheme.of(context).textSecondary,
        letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
      ),
    );

// ---------------------------------------------------------------------------
// SetPinDialog
// ---------------------------------------------------------------------------

/// Pops the chosen PIN, or null on cancel.
Future<String?> showSetPinDialog(BuildContext context) {
  return showTcDialog<String>(
    context: context,
    builder: (context) => const _SetPinContent(),
  );
}

class _SetPinContent extends StatefulWidget {
  const _SetPinContent();

  @override
  State<_SetPinContent> createState() => _SetPinContentState();
}

class _SetPinContentState extends State<_SetPinContent> {
  final _pin1 = TextEditingController();
  final _pin2 = TextEditingController();
  String? _error;

  @override
  void dispose() {
    _pin1.dispose();
    _pin2.dispose();
    super.dispose();
  }

  void _submit() {
    final p1 = _pin1.text.trim();
    final p2 = _pin2.text.trim();
    if (p1.length < pinMinLen) {
      setState(() => _error = 'PIN must be at least $pinMinLen digits.');
      return;
    }
    if (p1 != p2) {
      _pin2.clear();
      setState(() => _error = 'PINs do not match.');
      return;
    }
    Navigator.pop(context, p1);
  }

  @override
  Widget build(BuildContext context) {
    return TcDialogShell(
      title: 'Set PIN',
      width: 340,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(label: 'SET PIN', onPressed: _submit),
      ],
      children: [
        Text(
          'Choose a $pinMinLen–$pinMaxLen digit numeric PIN to lock your '
          'identity and message database.',
          style: TextStyle(
              fontSize: TCType.textBodySm, color: SectionTheme.of(context).textSecondary),
        ),
        const SizedBox(height: 12),
        _fieldLabel(context, 'NEW PIN'),
        const SizedBox(height: 6),
        _PinField(controller: _pin1, hint: 'New PIN', autofocus: true),
        const SizedBox(height: 10),
        _fieldLabel(context, 'CONFIRM PIN'),
        const SizedBox(height: 6),
        _PinField(controller: _pin2, hint: 'Confirm PIN', onSubmitted: (_) => _submit()),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// ChangePinDialog
// ---------------------------------------------------------------------------

/// Pops a [PinChange] (newPin null = remove protection), or null on cancel.
/// [verifyPin] checks the current PIN, standing in for lockbox.unlock().
Future<PinChange?> showChangePinDialog(BuildContext context,
    {required bool Function(String pin) verifyPin}) {
  return showTcDialog<PinChange>(
    context: context,
    builder: (context) => _ChangePinContent(verifyPin: verifyPin),
  );
}

class _ChangePinContent extends StatefulWidget {
  const _ChangePinContent({required this.verifyPin});

  final bool Function(String pin) verifyPin;

  @override
  State<_ChangePinContent> createState() => _ChangePinContentState();
}

class _ChangePinContentState extends State<_ChangePinContent> {
  final _current = TextEditingController();
  final _new1 = TextEditingController();
  final _new2 = TextEditingController();
  String? _error;

  @override
  void dispose() {
    _current.dispose();
    _new1.dispose();
    _new2.dispose();
    super.dispose();
  }

  void _submit() {
    final current = _current.text.trim();
    if (current.length < pinMinLen) {
      setState(() => _error = 'Enter your current PIN.');
      return;
    }
    if (!widget.verifyPin(current)) {
      _current.clear();
      setState(() => _error = 'Current PIN is incorrect.');
      return;
    }

    final new1 = _new1.text.trim();
    final new2 = _new2.text.trim();
    if (new1.isEmpty && new2.isEmpty) {
      Navigator.pop(context, const PinChange(newPin: null));
      return;
    }
    if (new1.length < pinMinLen) {
      setState(() => _error =
          'New PIN must be at least $pinMinLen digits (or leave blank to remove).');
      return;
    }
    if (new1 != new2) {
      _new2.clear();
      setState(() => _error = 'New PINs do not match.');
      return;
    }
    Navigator.pop(context, PinChange(newPin: new1));
  }

  @override
  Widget build(BuildContext context) {
    return TcDialogShell(
      title: 'Change PIN',
      width: 360,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(label: 'APPLY', onPressed: _submit),
      ],
      children: [
        Text(
          'Enter your current PIN, then set a new one. Leave the new PIN '
          'fields empty to remove PIN protection.',
          style: TextStyle(
              fontSize: TCType.textBodySm, color: SectionTheme.of(context).textSecondary),
        ),
        const SizedBox(height: 12),
        _fieldLabel(context, 'CURRENT PIN'),
        const SizedBox(height: 6),
        _PinField(controller: _current, hint: 'Current PIN', autofocus: true),
        const SizedBox(height: 10),
        _fieldLabel(context, 'NEW PIN'),
        const SizedBox(height: 6),
        _PinField(controller: _new1, hint: 'New PIN (leave blank to remove)'),
        const SizedBox(height: 10),
        _fieldLabel(context, 'CONFIRM NEW PIN'),
        const SizedBox(height: 6),
        _PinField(controller: _new2, hint: 'Confirm new PIN', onSubmitted: (_) => _submit()),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// UnlockDialog
// ---------------------------------------------------------------------------

/// Pops true when [verifyPin] accepts the entered PIN. Enforces the Qt
/// dialog's 5-attempt limit and 30-second cooldown.
Future<bool?> showUnlockDialog(BuildContext context,
    {required bool Function(String pin) verifyPin}) {
  return showTcDialog<bool>(
    context: context,
    builder: (context) => _UnlockContent(verifyPin: verifyPin),
  );
}

class _UnlockContent extends StatefulWidget {
  const _UnlockContent({required this.verifyPin});

  final bool Function(String pin) verifyPin;

  @override
  State<_UnlockContent> createState() => _UnlockContentState();
}

class _UnlockContentState extends State<_UnlockContent> {
  final _pin = TextEditingController();
  int _attempts = 0;
  int _cooldownRemaining = 0;
  Timer? _cooldownTimer;
  String? _error;

  @override
  void dispose() {
    _cooldownTimer?.cancel();
    _pin.dispose();
    super.dispose();
  }

  bool get _coolingDown => _cooldownRemaining > 0;

  void _startCooldown() {
    _cooldownRemaining = unlockCooldownSecs;
    _cooldownTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      setState(() {
        _cooldownRemaining -= 1;
        if (_cooldownRemaining <= 0) {
          timer.cancel();
          _error = null;
        } else {
          _error = 'Too many attempts. Wait ${_cooldownRemaining}s.';
        }
      });
    });
  }

  void _submit() {
    if (_coolingDown) return;
    final pin = _pin.text.trim();
    if (pin.length < pinMinLen) {
      setState(() => _error = 'PIN must be $pinMinLen–$pinMaxLen digits.');
      return;
    }
    if (widget.verifyPin(pin)) {
      Navigator.pop(context, true);
      return;
    }
    _pin.clear();
    _attempts += 1;
    final remaining = maxUnlockAttempts - _attempts;
    setState(() {
      if (remaining > 0) {
        _error = 'Incorrect PIN. $remaining attempt(s) remaining.';
      } else {
        _attempts = 0;
        _error = 'Too many attempts. Wait ${unlockCooldownSecs}s.';
        _startCooldown();
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return TcDialogShell(
      title: 'TrenchChat — Unlock',
      width: 340,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'QUIT', onPressed: () => Navigator.pop(context, false)),
        TcPrimaryButton(label: 'UNLOCK', onPressed: _coolingDown ? null : _submit),
      ],
      children: [
        Center(
          child: Text(
            'TrenchChat is locked',
            style: TextStyle(
              fontFamily: SectionTheme.styleOf(context).displayFont,
              fontSize: TCType.textDisplaySm,
              color: tc.textEmphasis,
              shadows: tcTextGlow(context),
            ),
          ),
        ),
        const SizedBox(height: 6),
        Center(
          child: Text(
            'Enter your PIN to unlock.',
            style: TextStyle(
                fontSize: TCType.textBodySm, color: SectionTheme.of(context).textSecondary),
          ),
        ),
        const SizedBox(height: 14),
        _PinField(
          controller: _pin,
          hint: 'Enter PIN',
          autofocus: true,
          enabled: !_coolingDown,
          onSubmitted: (_) => _submit(),
        ),
      ],
    );
  }
}
