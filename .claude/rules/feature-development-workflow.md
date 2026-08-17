# Feature Development Workflow

New core functionality is prototyped in `devtools/testenv/` against a real
two-peer network before it's wired into a client UI. This catches protocol
and manager-level bugs against real RNS Links, real path resolution, and
real timing — the same class of bug the pytest suite's in-process
`TestTransport` shim can mask by delivering messages instantly and
synchronously.

The active client is the Flutter app (`flutter_ui/`), which consumes
`devtools/testenv/api.py` directly — so for it, "the endpoint" and "the
client wiring" are the same layer, and step 3's Qt port only applies when
deliberately maintaining the legacy Qt GUI.

## The required shape

Every feature that mutates state must follow this call structure, so the
same code path runs whether it's driven by the disposable test UI or the
real GUI:

1. **Business logic goes in `trenchchat/core/actions.py`.** If a feature
   needs more than one manager call — a permission check before a
   mutation, a computed recipient list, a create-then-follow-up-call
   sequence — it's a plain function here taking already-constructed
   managers as arguments. See `create_channel`, `join_public_channel`,
   `compute_channel_recipients` for the established shape. Never put this
   sequencing inline in a GUI handler or an API endpoint.
2. **`trenchchat/gui/main_window.py`'s `_on_*` handlers call that
   function.** They keep only the Qt-specific bits (dialogs, message
   boxes, widget refreshes) and delegate everything else to `actions.py`.
3. **`devtools/testenv/api.py`'s endpoints call the same function** — not
   a parallel reimplementation. This is what makes a bug caught in the
   test environment a real bug, and a feature proven there ready to port.
4. **New core managers are instantiated in
   `devtools/testenv/backend_core.py`'s `Backend.__init__`**, mirroring
   `main.py`'s wiring order exactly (identity → storage → router →
   managers). If `main.py` constructs it with
   `ManagerX(identity, storage, router)`, `Backend` does too.

Before writing new logic, check `trenchchat/core/` for an existing
manager/action that already does it — a missing piece in the test
environment (a manager never instantiated, an endpoint never written)
means it hasn't been ported yet, not that a new design is needed.

## Workflow

1. Implement the feature per the shape above: `actions.py` (or a core
   manager method it calls) + a `devtools/testenv/api.py` endpoint +
   `Backend` wiring if a new manager is involved.
2. Verify it over a real two-process network:
   ```bash
   .venv/bin/python devtools/testenv/orchestrator.py
   # visit http://localhost:8800/, drive the feature from both panes
   ```
   or, for a scripted check without the UI, extend/run
   `devtools/testenv/smoke_test.py`.
3. Only after it works against real Reticulum Links between two
   independent identities, wire the feature into the Flutter client
   (`lib/api/client.dart` + `lib/app_state.dart` + screen, with a widget
   test) — and, only if the legacy Qt GUI is being kept current for this
   feature, port the same `actions.py` call into
   `trenchchat/gui/main_window.py`'s `_on_*` handler — GUI-specific
   plumbing only, no reimplemented logic (see `.claude/rules/gui-conventions.md`).
4. Add the pytest coverage required by
   `.claude/rules/test-coverage-for-new-features.md` and
   `.claude/rules/permission-enforcement.md` (if the feature touches a
   permission) in the matching `tests/test_*.py` file, and run the full
   suite per `.claude/rules/run-tests-after-changes.md`.

The pytest suite and the test environment are complementary, not
redundant: pytest is the fast, deterministic specification (real
managers, simulated transport); the test environment is the slow, honest
one (real managers, real network). A feature isn't done until both agree.

## Known real-app quirks the test environment surfaces

These are genuine behaviors of the production code, not harness bugs —
worth knowing before chasing a phantom bug in new work:

- **Invites/join-requests don't retry.** Unlike chat messages,
  `invite.py`'s `_send_raw()` has no pending-retry queue. If the
  recipient's network path isn't resolved at the exact instant you
  invite/accept, the message is silently dropped.
- **No delivery-ordering guarantee.** Two independent LXMF sends (e.g. a
  member-list update immediately followed by a chat message) can arrive
  out of order. `messaging.py` drops a chat message if the receiver isn't
  yet marked subscribed/member locally.
- **Real network round trips are slow compared to the pytest suite.** A
  chain like invite → join request → member-list update → sync request →
  sync response is four separate hops, not one — give it several seconds
  before concluding something didn't work.

Full detail in `devtools/testenv/README.md`.
