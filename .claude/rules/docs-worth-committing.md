# Which Docs Get Committed

A document earns a place in `docs/` when it holds reasoning that cannot be
recovered by reading the code. It does not earn one by describing work that
is already visible in the diff.

Default to **no new file**. Most of what feels like it needs a document
belongs in a docstring, a commit message, or a PR description instead.

## Write it down when it is

- **A trust model or threat model.** Which attacker is assumed, what the
  network guarantees, what it refuses to. Nothing in the code states this,
  and every later decision depends on it.
- **A rejected alternative.** The design that looks obvious, was tried or
  considered, and lost — and why. Without it the next person re-proposes it.
- **A deliberate non-fix.** A known gap left open on purpose, with the trade
  that made leaving it correct. This is the highest-value thing in `docs/`;
  it is also the thing most often lost.
- **A cross-cutting mechanism no single file owns.** Offline sync spans four
  modules and three mechanisms; no one file can explain it.
- **Ground confirmed sound.** So the same code is not re-audited as a finding
  next time.

## Do not write it down when it is

- **A description of what the code does.** The code says that, and it stays
  true when the code changes.
- **A plan for work now finished.** Task lists, phased rollouts, effort
  estimates, "recommended priority" — delete these when the work lands.
- **A proposal whose decisions have been made.** Once implemented, fold any
  durable reasoning into the docstring beside the code and delete the
  proposal. A spec that must match the code byte-for-byte (a signing digest,
  a wire format) belongs *next to that code*, never in a second copy that
  drifts.
- **A test plan.** Once the tests exist, they are the plan.
- **A per-change changelog.** That is what git history is.

## Keep one living document per area, not one per event

`security-improvements.md` is the record of what is fixed and what is open;
it gets edited, not superseded. A dated audit or review sits beside it only
for what a living record cannot carry — the trust model it judged against and
the ground it cleared. When a document's subject is finished, delete the
document rather than leaving it to be mistaken for current.

## Before adding to `docs/`

1. Can this be a docstring next to the code it describes? Put it there.
2. Is it reasoning, or narration of the diff? Narration goes in the commit
   message.
3. Will it be wrong in three months if nobody updates it? Then either it
   belongs beside the code, or it needs a clear as-of date and scope.
