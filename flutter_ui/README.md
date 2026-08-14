# flutter_ui

A Flutter client for TrenchChat, talking to one tester's `devtools/testenv/api.py`
backend over REST + a `/ws` event stream. This is a spike UI, not a replacement for
the Qt client -- see the repo root `CLAUDE.md` for the overall project shape.

Windows desktop is the only configured platform (`windows/` exists; no android,
ios, web, or macos, or linux).

## Running it

Start the backend first, then the Flutter app.

```bash
# from the repo root
.venv/Scripts/python devtools/testenv/orchestrator.py --testers 2

# from flutter_ui/, in another terminal
flutter run
```

`orchestrator.py --testers 2` spins up two independent tester backends so you can
drive both sides of a conversation. The Flutter app defaults to tester A --
`baseUrl` is hardcoded to `http://127.0.0.1:8801` in `lib/main.dart`. To point it at
tester B (or any other tester), pass `TrenchChatApp(baseUrl: 'http://127.0.0.1:8802')`
from a modified `main()`; there's no runtime flag for this yet.

### Ports

- `8800` -- orchestrator web UI (a browser-based test harness, unrelated to this app)
- `8801`, `8802`, ... -- one FastAPI (REST + WebSocket) per tester, in start order
- `41001` -- the Reticulum hub's TCP listener the testers connect through

## Tests

```bash
flutter test
```

Three kinds of coverage live under `test/`:

- `test/tokens_test.dart` -- re-declares every design token independently and
  compares against `lib/theme/tokens.dart`. Don't invent a token value to make this
  pass; port it from the source CSS instead (see the comment at the top of
  `tokens.dart`).
- `test/screens/*_test.dart` -- widget tests for layout and behavior.
- `test/golden/*_test.dart` -- pixel-diffed against PNGs in `test/golden/goldens/`.

Golden tests build a real `AppState` against a dead port (65500) and rely on staying
fully offline during the build: the WebSocket connects lazily (`lib/api/ws.dart`),
and `test/golden/fixtures.dart`'s `populateFixtureState()` pre-seeds `avatarCache`
with nulls so no widget reaches for a real HTTP request. If you add a fetch that
runs during `build()` or `AppState` construction, the golden suite will hang or
flake -- keep new fetches behind explicit user action or `init()`.

If you touch anything the goldens render, regenerate and eyeball the diff before
committing:

```bash
flutter test --update-goldens
```

## Conventions

- **State management**: one `ChangeNotifier` (`lib/app_state.dart`), no
  Provider/Riverpod/Bloc. See the comment at the top of that file for why.
- **Design tokens are locked**: only `lib/theme/tokens.dart` may define token values,
  and only by porting one from the design system's CSS.
- **Dialogs**: `lib/widgets/tc_dialog.dart`'s `showTcDialog` + `TcDialogShell` is the
  one dialog pattern in the app -- see `lib/screens/dialogs/` for examples. Don't
  reach for a raw Material `AlertDialog`; the app theme sets no `dialogTheme`, so it
  renders visually wrong for this design language.
- **API errors**: `lib/api/client.dart`'s `ApiClient` throws `ApiException` for any
  non-2xx response. `AppState`'s mutating methods (`createServer`, `sendMessage`,
  ...) catch it and set `actionError`, one surface for every action's failure.
