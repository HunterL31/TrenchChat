// Golden for the whole icon pack: every catalog entry at 16px and 32px so a
// geometry change to any glyph shows up as a pixel diff.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/theme/app_theme.dart';
import 'package:flutter_ui/theme/tokens.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

import 'test_fonts.dart';

void main() {
  setUpAll(loadTestFonts);

  testWidgets('icon pack at 16px and 32px', (tester) async {
    await tester.pumpWidget(MaterialApp(
      theme: buildAppTheme(),
      debugShowCheckedModeBanner: false,
      home: Scaffold(
        body: Center(
          child: Container(
            key: const Key('golden-grid'),
            color: TCColors.bgApp,
            padding: const EdgeInsets.all(16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                for (final size in const [16.0, 32.0])
                  Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        for (final icon in TcIcons.all)
                          Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 6),
                            child: Column(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                TcIcon(icon, size: size, color: TCColors.textPrimary),
                                const SizedBox(height: 4),
                                Text(
                                  icon.name,
                                  style: TextStyle(fontSize: 8, color: TCColors.textSecondary),
                                ),
                              ],
                            ),
                          ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    ));
    await tester.pump();
    await expectLater(
      find.byKey(const Key('golden-grid')),
      matchesGoldenFile('goldens/icon_pack.png'),
    );
  });
}
