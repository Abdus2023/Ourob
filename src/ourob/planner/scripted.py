"""A deterministic, file-backed planner.

Plans are JSON documents checked into ``plans/``.  Running a plan is
reproducible: the same plan against the same tree does the same thing, which is
what makes a self-modification reviewable before it is ratified.

Plan format::

    {
      "goal": "Add a rot13 skill to the runtime",
      "max_steps": 12,
      "stop_on_error": false,
      "steps": [
        {"skill": "write_file", "args": {"path": "...", "content": "..."},
         "rationale": "why this step exists",
         "skip_if_failed": ["previous"]},
        {"skill": "run_verification", "args": {}}
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from ..state.model import Invocation
from .base import Planner, RuntimeView


@dataclass
class PlannedStep:
    skill: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    skip_if_failed: list[str] = field(default_factory=list)
    repeat: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlannedStep:
        if "skill" not in data:
            raise ConfigError(f"plan step is missing 'skill': {data!r}")
        return cls(
            skill=str(data["skill"]),
            args=dict(data.get("args") or {}),
            rationale=str(data.get("rationale", "")),
            skip_if_failed=[str(s) for s in data.get("skip_if_failed", [])],
            repeat=max(1, int(data.get("repeat", 1))),
        )


@dataclass
class Plan:
    goal: str
    steps: list[PlannedStep]
    max_steps: int = 32
    stop_on_error: bool = False
    source: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "") -> Plan:
        if "goal" not in data:
            raise ConfigError("a plan must state a goal")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ConfigError("a plan must contain a non-empty 'steps' list")
        plan = cls(
            goal=str(data["goal"]),
            steps=[PlannedStep.from_dict(s) for s in raw_steps],
            max_steps=int(data.get("max_steps", 32)),
            stop_on_error=bool(data.get("stop_on_error", False)),
            source=source,
        )
        return plan

    @classmethod
    def load(cls, path: Path) -> Plan:
        path = Path(path)
        if not path.is_file():
            raise ConfigError(f"no such plan: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
        return cls.from_dict(data, source=path.as_posix())

    @property
    def step_count(self) -> int:
        return sum(step.repeat for step in self.steps)


class ScriptedPlanner(Planner):
    """Replays a :class:`Plan` one invocation at a time."""

    name = "scripted"

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self._expanded: list[PlannedStep] = [
            step for step in plan.steps for _ in range(step.repeat)
        ]
        self._cursor = 0

    def next_action(self, view: RuntimeView) -> Invocation | None:
        while self._cursor < len(self._expanded):
            step = self._expanded[self._cursor]
            self._cursor += 1
            if "previous" in step.skip_if_failed:
                last = view.last()
                if last is not None and (not last.ok or last.denied):
                    continue
            return Invocation(
                skill=step.skill, args=dict(step.args), rationale=step.rationale
            )
        return None

    def describe(self) -> str:
        return f"scripted[{len(self._expanded)} steps from {self.plan.source or '<inline>'}]"
