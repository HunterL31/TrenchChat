# Multi-Peer Scenario Testing

`devtools/testenv/scenarios/` runs up to four real Backends in separate OS
processes over real RNS Links. Each scenario is one row of
`docs/testenv-scenarios.md`: a sequence of client actions and the state every
peer must converge on.

It complements pytest rather than duplicating it. **pytest is the fast,
deterministic specification** — real managers, simulated transport, messages
delivered instantly. **The scenario suite is the slow, honest one** — real
managers, real network, real timing. A feature is not done until both agree,
and every defect the scenarios have found so far was invisible to pytest
precisely because pytest's transport never loses a race.

```bash
.venv/bin/python devtools/testenv/scenarios/runner.py --family sync
.venv/bin/python devtools/testenv/scenarios/runner.py --scenario sync2 --repeat 10
.venv/bin/python devtools/testenv/scenarios/runner.py --link-profile lora_fast
.venv/bin/python devtools/testenv/scenarios/runner.py --scenario sync2 --tester-log /tmp/c2.log
```

## When a scenario is the right tool

Add one when the behaviour depends on something pytest's shim cannot produce:

- **More than two peers**, or a peer that is both requester and responder.
- **Timing and ordering** — anything driven by announces, retries, or a
  cooldown, where "not yet" and "never" are only distinguishable by waiting.
- **Absence and return** — a link dropping, a process dying, an identity
  wiped, a peer that never comes back.
- **Real paths** — anything that behaves differently when `Identity.recall()`
  has not resolved yet. Several real defects live only here.
- **Degraded links** — `--link-profile` shapes bitrate, latency and loss. A
  constant that is comfortable at 100 Mbps can be badly wrong at 1 kbps.
- **The API surface** (the `api` family) — the token, CORS, the event socket.

Do **not** add one for logic pytest already covers well: permission
enforcement (that is `tests/test_adversarial.py`), storage behaviour, digest
and wire-format contracts, or anything single-peer. A scenario costs 20–200
seconds and a full environment reset; a pytest test costs milliseconds.

## Adding one

1. **Pick a family and the next free number** — `public`, `invite`, `sync`,
   `links`, `servers`, `social`, `restart`, `voice`, `api`, `integrity`. An ID
   is the family name plus a number: `sync11`, `api4`. A genuinely new area
   gets a new family and a new `scen_*.py`.
2. **Decide strict or probe.** `STRICT` means the expected result is settled
   behaviour and failing is a bug. `PROBE` means it is a *prediction* about
   behaviour nothing covers yet: it records what happened and never fails the
   run. Write a probe when you are documenting a suspected gap, and promote it
   to strict once the behaviour is settled — integrity2 was a probe that confirmed a
   real gap, then became strict when the gap was fixed.
3. **Write the body in the matching `scen_*.py`**, using `flows.py` for setup
   and `asserts.py` for every check. Register the module's import in
   `runner.py` if the family is new.
4. **Add the row to `docs/testenv-scenarios.md`** with the measured result,
   not the hoped-for one.
5. **Run it, and run it more than once** (see below).

## Rules the suite learned the hard way

**Never assert before the precondition has converged.** A send is addressed to
whatever the subscriber set holds at that moment, so asserting fan-out before
the owner has registered the joiners tests subscribe latency, not fan-out. Use
`flows.public_channel` / `join_all(..., owner)` and
`asserts.subscribers_converged`, which wait for exactly that.

**Never sleep; always poll.** `wait_until` for "this must happen", `settle`
for "record whether it happened", `hold_for` for "this must *not* arrive".
A fixed sleep either flakes or wastes the run.

**A single pass is not a fix.** This is the mistake that cost the most here:
sync11 was recorded as fixed on one 31.7s pass and was still failing four runs
in five. Before claiming a fix, `--repeat` it — five runs minimum for
something that was intermittent, and say the ratio rather than "it passes".
The same applies in reverse: one failure of a passing scenario is a flake
report, not a regression, until it repeats.

**Record refuted predictions.** A probe that disproves its own hypothesis is a
result worth keeping — it stops the same theory being re-proposed. Both public10
and social3 predicted a gap and found working recovery.

**Distrust a green run that proves nothing.** A scenario that passes because
its assertion is unreachable is worse than no scenario. If a fix is meant to
change behaviour, confirm the test fails without it.

## When a scenario fails

Read the failure before theorising. The suite's silent-failure modes all look
identical from outside — refused, throttled, dropped on an unresolved path,
and never sent all present as "nobody answered".

1. `--tester-log` captures every tester's RNS output at debug level. Refusals
   in `sync.py` are logged there and nowhere else.
2. If the log cannot tell you *why*, add the log line rather than guessing —
   a silent `return` in a handler is a bug in its own right, since the peer on
   the other side cannot distinguish it from packet loss either.
3. Attribute before concluding. The log merges every tester and every repeat,
   so make the failure message name the identities involved.
4. Fix the implementation, never the scenario — the same rule as
   `tests-as-specification.md`. A scenario weakened to pass is a defect with a
   green light on it.

## Where the result belongs

`docs/testenv-scenarios.md` is the living record: the matrix, the measured
timings, and the findings split into fixed, open, and refuted. Keep it honest
about what is *not* fixed — a deliberate deferral recorded as such ("public
channel gaps, left by decision") is worth more than an open lead nobody
intends to take. When a scenario finds a real defect, the fix also needs a
pytest regression test: the scenario proves it happens on a real network, and
pytest is what keeps it from coming back.
