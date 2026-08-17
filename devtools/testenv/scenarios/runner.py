"""
Scripted multi-peer scenario runner for the dev test environment.

Spawns orchestrator.py, waits for every tester's API, then runs scenarios
against real Backends in separate OS processes over real RNS Links. Each
scenario starts from a wiped environment.

    python devtools/testenv/scenarios/runner.py                 # every scenario
    python devtools/testenv/scenarios/runner.py --family A
    python devtools/testenv/scenarios/runner.py --scenario A5 A6
    python devtools/testenv/scenarios/runner.py --attach        # use a running orchestrator
    python devtools/testenv/scenarios/runner.py --json out.json

Exits 0 if every strict scenario passed. Probes never fail the run -- they
report what happened.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_SCENARIOS_DIR = Path(__file__).resolve().parent
_TESTENV_DIR = _SCENARIOS_DIR.parent
for _p in (str(_SCENARIOS_DIR), str(_TESTENV_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from asserts import ScenarioFailure  # noqa: E402
from peer import Orchestrator, Peer  # noqa: E402
from scenario import PROBE, REGISTRY, Result  # noqa: E402

import scen_public  # noqa: F401,E402  (registers family A)

_ORCHESTRATOR = _TESTENV_DIR / "orchestrator.py"
_BOOT_TIMEOUT = 180.0
_RESET_TIMEOUT = 180.0


class Env:
    """The tester roster a scenario runs against."""

    def __init__(self, peers: dict[str, Peer]):
        self._peers = peers

    def peers(self, *tags: str) -> tuple[Peer, ...]:
        return tuple(self._peers[t] for t in tags)

    def all(self) -> list[Peer]:
        return list(self._peers.values())

    def close(self) -> None:
        for p in self._peers.values():
            p.close()


def _wait(pred, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(1.0)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


def _boot(testers: int) -> subprocess.Popen:
    """Launch the orchestrator in its own process group.

    Terminating the orchestrator alone leaves the hub and every worker it
    spawned running, which then holds the ports the next run preflights
    against. Killing the group reaps all of them.
    """
    print(f"starting orchestrator with {testers} testers...")
    return subprocess.Popen(
        [sys.executable, str(_ORCHESTRATOR), "--testers", str(testers)],
        cwd=str(_TESTENV_DIR),
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _teardown(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    group = os.getpgid(proc.pid)
    os.killpg(group, signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _wait_environment(orch: Orchestrator, testers: int) -> Env:
    _wait(orch.up, "the orchestrator to answer", _BOOT_TIMEOUT)
    _wait(lambda: all(t["alive"] for t in orch.status()["testers"].values()),
          "every tester process to be alive", _BOOT_TIMEOUT)

    config = orch.config()["testers"]
    if len(config) != testers:
        raise RuntimeError(f"orchestrator launched {len(config)} testers, expected {testers}")
    peers = {t["tag"]: Peer(t["tag"], t["api_port"]) for t in config}
    for p in peers.values():
        _wait(p.alive, f"{p.tag}'s API", _BOOT_TIMEOUT)
        p.forget_hash()
    return Env(peers)


def _reset(orch: Orchestrator, env: Env) -> None:
    """Wipe every tester back to a fresh identity between scenarios."""
    orch.reset()
    _wait(lambda: all(t["alive"] for t in orch.status()["testers"].values()),
          "every tester to relaunch", _RESET_TIMEOUT)
    for p in env.all():
        _wait(p.alive, f"{p.tag}'s API after reset", _RESET_TIMEOUT)
        p.forget_hash()


def _select(family: str | None, ids: list[str] | None) -> list:
    chosen = list(REGISTRY.values())
    if family:
        chosen = [s for s in chosen if s.family == family.upper()]
    if ids:
        wanted = {i.upper() for i in ids}
        unknown = wanted - set(REGISTRY)
        if unknown:
            raise SystemExit(f"unknown scenario id(s): {sorted(unknown)}")
        chosen = [s for s in chosen if s.id in wanted]
    if not chosen:
        raise SystemExit("no scenarios matched")
    return sorted(chosen, key=lambda s: s.id)


def _run_one(scen, env: Env) -> Result:
    started = time.time()
    try:
        notes = scen.fn(env) or {}
        status = "surprise" if notes.get("surprise") else "pass"
        return Result(scen.id, scen.title, scen.kind, status,
                      time.time() - started, notes.get("surprise", ""), notes)
    except ScenarioFailure as e:
        return Result(scen.id, scen.title, scen.kind, "fail", time.time() - started, str(e))
    except Exception as e:
        return Result(scen.id, scen.title, scen.kind, "error", time.time() - started,
                      f"{type(e).__name__}: {e}")


_MARK = {"pass": "PASS", "surprise": "NOTE", "fail": "FAIL", "error": "ERR "}


def _report(results: list[Result]) -> bool:
    print("\n" + "=" * 72)
    print("SCENARIO RESULTS")
    print("=" * 72)
    for r in results:
        kind = " (probe)" if r.kind == PROBE else ""
        print(f"{_MARK[r.status]}  {r.id}  {r.title}{kind}  [{r.duration:.0f}s]")
        if r.detail:
            print(f"      {r.detail}")
        for key, value in r.notes.items():
            if key != "surprise":
                print(f"      {key}: {value}")

    strict = [r for r in results if r.kind != PROBE]
    failed = [r for r in strict if r.status in ("fail", "error")]
    probes = [r for r in results if r.kind == PROBE]
    print("-" * 72)
    print(f"strict: {len(strict) - len(failed)}/{len(strict)} passed | probes: {len(probes)}")
    if failed:
        print(f"failed: {', '.join(r.id for r in failed)}")
    return not failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Run testenv multi-peer scenarios")
    parser.add_argument("--testers", type=int, default=4)
    parser.add_argument("--family", help="run one family, e.g. A")
    parser.add_argument("--scenario", nargs="+", help="run specific matrix IDs")
    parser.add_argument("--attach", action="store_true",
                        help="use an already-running orchestrator instead of spawning one")
    parser.add_argument("--json", help="write results to this path")
    args = parser.parse_args()

    chosen = _select(args.family, args.scenario)
    orch = Orchestrator()
    proc: subprocess.Popen | None = None
    env: Env | None = None
    results: list[Result] = []

    try:
        if not args.attach:
            if orch.up():
                raise SystemExit("an orchestrator is already running on 8800; "
                                 "stop it or pass --attach")
            proc = _boot(args.testers)
        env = _wait_environment(orch, args.testers)
        print(f"environment ready; running {len(chosen)} scenario(s)\n")

        for i, scen in enumerate(chosen):
            if i > 0:
                print("  resetting environment...")
                _reset(orch, env)
            print(f"-> {scen.id}  {scen.title}")
            result = _run_one(scen, env)
            print(f"   {_MARK[result.status]} in {result.duration:.0f}s"
                  + (f" -- {result.detail}" if result.detail else ""))
            results.append(result)
    finally:
        if env is not None:
            env.close()
        orch.close()
        if proc is not None:
            _teardown(proc)

    ok = _report(results)
    if args.json:
        Path(args.json).write_text(json.dumps(
            [{"id": r.id, "title": r.title, "kind": r.kind, "status": r.status,
              "duration_secs": round(r.duration, 1), "detail": r.detail, "notes": r.notes}
             for r in results], indent=2))
        print(f"\nwrote {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
