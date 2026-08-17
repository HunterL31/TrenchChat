// Registers the bundled VT323 / IBM Plex Mono fonts with the test binding.
// flutter_test renders text with a placeholder font unless a family is
// explicitly loaded via FontLoader -- without this, goldens would show the
// wrong glyphs and mislead a reviewer comparing against the mockup.
import 'package:flutter/services.dart' show FontLoader, rootBundle;
import 'package:flutter_ui/theme/tokens.dart';

Future<void> loadTestFonts() async {
  final display = FontLoader(TCType.fontDisplay)
    ..addFont(rootBundle.load('assets/fonts/VT323-Regular.ttf'));
  await display.load();

  final mono = FontLoader(TCType.fontMono)
    ..addFont(rootBundle.load('assets/fonts/IBMPlexMono-Regular.ttf'))
    ..addFont(rootBundle.load('assets/fonts/IBMPlexMono-Medium.ttf'))
    ..addFont(rootBundle.load('assets/fonts/IBMPlexMono-SemiBold.ttf'))
    ..addFont(rootBundle.load('assets/fonts/IBMPlexMono-Bold.ttf'))
    ..addFont(rootBundle.load('assets/fonts/IBMPlexMono-Italic.ttf'));
  await mono.load();
}
