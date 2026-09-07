"""Planner contracts.

The planner is the only component that *decides*.  Everything else -- skills,
policies, gates -- is deterministic machinery.  Keeping the deciding part behind
a narrow interface is what lets the same kernel be driven by a recorded script
(for tests and for reproducible self-modification) or by a language model
(for open-ended work) without changing a line of the safety machinery.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..state.model import Invocation

MAX_CONTEXT_OUTPUT = 1200


@dataclass
class Observation:
    """A compressed account of one completed step, for the planner's benefit."""

    index: int
    skill: str
    args: dict[str, Any]
    ok: bool
    output: str = ""
    error: str | None = None
    denied: bool = False
    denials: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        import json

        return json.dumps({"skill": self.skill, "args": self.args}, sort_keys=True, default=str)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "skill": self.skill,
            "args": self.args,
            "ok": self.ok,
            "denied": self.denied,
            "denials": self.denials,
            "output": self.output[-MAX_CONTEXT_OUTPUT:],
            "error": self.error,
        }

    def brief(self) -> str:
        status = "DENIED" if self.denied else ("ok" if self.ok else "error")
        tail = (self.error or self.output or "").strip().splitlines()
        snippet = tail[-1][:200] if tail else ""
        return f"#{self.index} {self.skill} -> {status} {snippet}"


@dataclass
class RuntimeView:
    """The planner's entire window onto the run."""

    goal: str
    run_id: str
    step_index: int
    budget_remaining: int
    skills: list[dict[str, Any]] = field(default_factory=list)
    history: list[Observation] = field(default_factory=list)
    policies: list[dict[str, Any]] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    amended_paths: list[str] = field(default_factory=list)

    def skill_names(self) -> list[str]:
        return [s["name"] for s in self.skills]

    def skill(self, name: str) -> dict[str, Any] | None:
        for entry in self.skills:
            if entry["name"] == name:
                return entry
        return None

    def last(self) -> Observation | None:
        return self.history[-1] if self.history else None

    def transcript(self) -> str:
        if not self.history:
            return "(no steps taken yet)"
        return "\n".join(obs.brief() for obs in self.history)


class Planner(ABC):
    """Chooses the next invocation, or ``None`` to end the run."""

    name: str = "planner"

    @abstractmethod
    def next_action(self, view: RuntimeView) -> Invocation | None:
        """Return the next action, or ``None`` when the goal is reached."""

    def describe(self) -> str:
        return self.name
