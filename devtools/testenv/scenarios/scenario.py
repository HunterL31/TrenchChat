"""
Scenario registration.

A scenario is one row of docs/testenv-scenarios.md: an ordered sequence of
client actions and the state every peer must converge on.

Two kinds:
  - "strict" — the expected result is settled behavior. Failing is a bug.
  - "probe"  — the expected result is a prediction about behavior no test
               covers yet. It records what actually happened and never fails
               the run; a surprise here is a finding, not a regression.
"""

import re
from dataclasses import dataclass, field
from typing import Callable

_FAMILY_RE = re.compile(r"[a-z]+")

STRICT = "strict"
PROBE = "probe"


@dataclass
class Scenario:
    id: str
    title: str
    kind: str
    fn: Callable
    peers: tuple[str, ...]

    @property
    def family(self) -> str:
        """The name before the number: "sync" in "sync11"."""
        return _FAMILY_RE.match(self.id).group(0)

    @property
    def number(self) -> int:
        return int(self.id[len(self.family):])


REGISTRY: dict[str, Scenario] = {}


def scenario(scenario_id: str, title: str, *, peers: str = "ABCD", kind: str = STRICT):
    """Register a scenario function under its matrix ID."""
    def decorator(fn):
        if scenario_id in REGISTRY:
            raise ValueError(f"duplicate scenario id {scenario_id}")
        REGISTRY[scenario_id] = Scenario(
            id=scenario_id, title=title, kind=kind, fn=fn, peers=tuple(peers),
        )
        return fn
    return decorator


@dataclass
class Result:
    """One scenario's outcome, plus whatever it chose to measure."""
    id: str
    title: str
    kind: str
    status: str            # pass | fail | error | surprise
    duration: float
    detail: str = ""
    notes: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """A probe never fails the run, however it turned out."""
        return self.status in ("pass", "surprise") or self.kind == PROBE
