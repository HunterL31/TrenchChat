"""
Scripted multi-peer scenario runner for the dev test environment.

Spawns orchestrator.py, waits for every tester's API, then runs scenarios
against real Backends in separate OS processes over real RNS Links. Each
scenario starts from a wiped environment.

    python devtools/testenv/scenarios/runner.py                 # every scenario
    python devtools/testenv/scenarios/runner.py --family public
    python devtools/testenv/scenarios/runner.py --scenario public5 public6
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

from asserts import set_timeout_scale, ScenarioFailure  # noqa: E402
from peer import Orchestrator, Peer  # noqa: E402
from scenario import PROBE, REGISTRY, Result  # noqa: E402

import scen_public  # noqa: F401,E402  (registers family public)
import scen_sync    # noqa: F401,E402  (registers family sync)
import scen_invite  # noqa: F401,E402  (registers family invite)
import scen_links   # noqa: F401,E402  (registers family links)
import scen_servers # noqa: F401,E402  (registers family servers)
import scen_social  # noqa: F401,E402  (registers family social)
import scen_restart # noqa: F401,E402  (registers family restart)
import scen_voice   # noqa: F401,E402  (registers family voice)
import scen_api     # noqa: F401,E402  (registers family api)
import scen_authorship  # noqa: F401,E402  (registers family integrity)

_ORCHESTRATOR = _TESTENV_DIR / "orchestrator.py"
_BOOT_TIMEOUT = 180.0
_RESET_TIMEOUT = 180.0


class Env:
    """The tester roster a scenario runs against, plus process control."""

    def __init__(self, peers: dict[str, Peer], orch: Orchestrator):
        self._peers = peers
        self.orch = orch

    def peers(self, *tags: str) -> tuple[Peer, ...]:
        return tuple(self._peers[t] for t in tags)

    def wait_alive(self, peer: Peer, timeout: float = 120.0) -> None:
        """Wait for a tester's API after a kill/start cycle."""
        _wait(peer.alive, f"{peer.tag}'s API to come back", timeout)

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


def _boot(testers: int, log_path: str | None = None) -> subprocess.Popen:
    """Launch the orchestrator in its own process group.

    Terminating the orchestrator alone leaves the hub and every worker it
    spawned running, which then holds the ports the next run preflights
    against. Killing the group reaps all of them.

    With log_path set, every tester's RNS output is captured at debug level.
    Refusals in sync.py are logged at debug and are silent on the wire, so
    that is the only way to see why a peer never answered.
    """
    print(f"starting orchestrator with {testers} testers...")
    env = dict(os.environ)
    sink = subprocess.DEVNULL
    if log_path:
        env["TC_TESTENV_LOGLEVEL"] = "7"
        sink = open(log_path, "w")
        print(f"  capturing tester logs to {log_path}")
    return subprocess.Popen(
        [sys.executable, str(_ORCHESTRATOR), "--testers", str(testers)],
        cwd=str(_TESTENV_DIR), env=env,
        stdout=sink, stderr=subprocess.STDOUT,
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

    token = orch.api_token()
    config = orch.config()["testers"]
    if len(config) != testers:
        raise RuntimeError(f"orchestrator launched {len(config)} testers, expected {testers}")
    peers = {t["tag"]: Peer(t["tag"], t["api_port"], token) for t in config}
    for p in peers.values():
        _wait(p.alive, f"{p.tag}'s API", _BOOT_TIMEOUT)
        p.forget_hash()
    return Env(peers, orch)


# How much slower a profile makes everything the scenarios wait on. Chosen
# from the measured links-family timings (broadband 5s -> lossy 102s for the same
# batch), rounded up so a pass fails on behaviour rather than on patience.
_PROFILE_SCALE = {
    "broadband": 1.0,
    "satellite": 2.0,
    "lossy": 6.0,
    "serial": 4.0,
    "lora_fast": 6.0,
    "lora_long": 10.0,
    "packet_radio": 10.0,
}


def _apply_link_profile(orch: Orchestrator, env: Env, profile: str) -> None:
    """Shape every tester, verifying the shaping actually took.

    Live shaping only: the matching bitrate hint written into each tester's RNS
    config is read at boot, so it stays stale for the run. The shaper still
    enforces the rate on the wire, which is the effect these passes are about.
    """
    status = orch.status()["testers"]
    for tag in status:
        orch.link_profile(tag, profile)
    applied = orch.status()["testers"]
    for tag, entry in applied.items():
        if entry["link_profile"] != profile:
            raise RuntimeError(f"{tag} did not take profile {profile!r}")
        if profile != "broadband" and entry["link_summary"] == "unshaped":
            raise RuntimeError(f"{tag} is still unshaped on {profile!r}")


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
        chosen = [s for s in chosen if s.family == family.lower()]
    if ids:
        wanted = {i.lower() for i in ids}
        unknown = wanted - set(REGISTRY)
        if unknown:
            raise SystemExit(f"unknown scenario id(s): {sorted(unknown)}")
        chosen = [s for s in chosen if s.id in wanted]
    if not chosen:
        raise SystemExit("no scenarios matched")
    # Natural order, so public10 follows public9 rather than public1.
    return sorted(chosen, key=lambda s: (s.family, s.number))


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
    parser.add_argument("--family", help="run one family, e.g. sync")
    parser.add_argument("--scenario", nargs="+", help="run specific matrix IDs")
    parser.add_argument("--attach", action="store_true",
                        help="use an already-running orchestrator instead of spawning one")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run the selection N times, to characterise a flake")
    parser.add_argument("--link-profile",
                        help="shape every tester to this profile before each scenario "
                             "(broadband, satellite, serial, lora_fast, lora_long, "
                             "packet_radio, lossy), re-running the same bodies on a radio")
    parser.add_argument("--timeout-scale", type=float, default=None,
                        help="multiply every assertion timeout; defaults to a value "
                             "chosen from --link-profile")
    parser.add_argument("--json", help="write results to this path")
    parser.add_argument("--tester-log",
                        help="capture every tester's RNS output at debug level here")
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
            proc = _boot(args.testers, args.tester_log)
        env = _wait_environment(orch, args.testers)
        scale = args.timeout_scale
        if scale is None:
            scale = _PROFILE_SCALE.get(args.link_profile or "broadband", 1.0)
        set_timeout_scale(scale)
        if args.link_profile:
            print(f"shaping every tester to {args.link_profile} "
                  f"(timeouts x{scale:g})")
            _apply_link_profile(orch, env, args.link_profile)
        print(f"environment ready; running {len(chosen)} scenario(s)\n")

        first = True
        for run in range(args.repeat):
            if args.repeat > 1:
                print(f"\n--- pass {run + 1}/{args.repeat} ---")
            for scen in chosen:
                if not first:
                    print("  resetting environment...")
                    _reset(orch, env)
                    if args.link_profile:
                        _apply_link_profile(orch, env, args.link_profile)
                first = False
                print(f"-> {scen.id}  {scen.title}")
                result = _run_one(scen, env)
                if args.repeat > 1:
                    result.id = f"{scen.id}#{run + 1}"
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
