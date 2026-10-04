"""The state model.

Everything the runtime knows about itself while it works is one of the records
below.  They are plain dataclasses with explicit ``to_dict``/``from_dict`` so
the journal on disk and the objects in memory are the same shape -- which is
what makes a run replayable and a crash recoverable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from .. import fsx
from ..clock import new_id, now, stamp

__all__ = [
    "GateResult",
    "Invocation",
    "PolicyDecision",
    "Run",
    "RunStatus",
    "SkillResult",
    "Step",
    "StepStatus",
    "VerificationReport",
    "jsonable",
    "new_id",
    "now",
    "stamp",
]


def jsonable(value: Any) -> Any:
    """Recursively convert to JSON-safe primitives."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict())
    return repr(value)


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    BLOCKED = "blocked"


class StepStatus(StrEnum):
    PLANNED = "planned"
    ALLOWED = "allowed"
    DENIED = "denied"
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class Invocation:
    """A single requested skill call."""

    skill: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    invocation_id: str = field(default_factory=lambda: new_id("inv"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "args": jsonable(self.args),
            "rationale": self.rationale,
            "invocation_id": self.invocation_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Invocation:
        return cls(
            skill=data["skill"],
            args=dict(data.get("args") or {}),
            rationale=data.get("rationale", ""),
            invocation_id=data.get("invocation_id", new_id("inv")),
        )


@dataclass
class PolicyDecision:
    policy: str
    allowed: bool
    reason: str
    severity: str = "info"

    def to_dict(self) -> dict[str, Any]:
        return jsonable(
            {
                "policy": self.policy,
                "allowed": self.allowed,
                "reason": self.reason,
                "severity": self.severity,
            }
        )


@dataclass
class SkillResult:
    """What a skill returned.  ``ok`` is authoritative; ``data`` is structured."""

    ok: bool
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return jsonable(
            {
                "ok": self.ok,
                "output": self.output,
                "data": self.data,
                "artifacts": self.artifacts,
                "error": self.error,
                "duration_ms": self.duration_ms,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillResult:
        return cls(
            ok=bool(data.get("ok")),
            output=data.get("output", ""),
            data=dict(data.get("data") or {}),
            artifacts=list(data.get("artifacts") or []),
            error=data.get("error"),
            duration_ms=int(data.get("duration_ms", 0)),
        )


@dataclass
class Step:
    index: int
    invocation: Invocation
    status: StepStatus = StepStatus.PLANNED
    decisions: list[PolicyDecision] = field(default_factory=list)
    result: SkillResult | None = None
    started_at: float = field(default_factory=now)
    ended_at: float | None = None

    @property
    def denied(self) -> bool:
        return self.status is StepStatus.DENIED

    def to_dict(self) -> dict[str, Any]:
        return jsonable(
            {
                "index": self.index,
                "status": self.status,
                "invocation": self.invocation,
                "decisions": self.decisions,
                "result": self.result,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
            }
        )


@dataclass
class GateResult:
    gate: str
    passed: bool
    blocking: bool = True
    summary: str = ""
    details: str = ""
    skipped: bool = False
    skip_reason: str = ""
    duration_ms: int = 0

    @property
    def failed_blocking(self) -> bool:
        return self.blocking and not self.passed and not self.skipped

    def to_dict(self) -> dict[str, Any]:
        return jsonable(
            {
                "gate": self.gate,
                "passed": self.passed,
                "blocking": self.blocking,
                "summary": self.summary,
                "details": self.details,
                "skipped": self.skipped,
                "skip_reason": self.skip_reason,
                "duration_ms": self.duration_ms,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateResult:
        return cls(**{k: data.get(k, v) for k, v in {
            "gate": "", "passed": False, "blocking": True, "summary": "",
            "details": "", "skipped": False, "skip_reason": "", "duration_ms": 0,
        }.items() if k in data})


@dataclass
class VerificationReport:
    run_id: str
    gates: list[GateResult] = field(default_factory=list)
    duration_ms: int = 0
    repo_digest: str = ""
    tree_digest: str = ""
    created_at: float = field(default_factory=now)

    @property
    def passed(self) -> bool:
        return not any(g.failed_blocking for g in self.gates)

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gates if g.failed_blocking]

    @property
    def digest(self) -> str:
        evidence = {
            "gates": [gate.to_dict() for gate in self.gates],
            "repo_digest": self.repo_digest,
            "tree_digest": self.tree_digest,
        }
        return fsx.sha256_text(json.dumps(evidence, sort_keys=True, separators=(",", ":")))

    def summary_line(self) -> str:
        total = len(self.gates)
        skipped = sum(1 for g in self.gates if g.skipped)
        failed = len(self.failures)
        return f"{total - failed - skipped}/{total} gates passed, {skipped} skipped, {failed} failed"

    def to_dict(self) -> dict[str, Any]:
        return jsonable(
            {
                "run_id": self.run_id,
                "gates": self.gates,
                "duration_ms": self.duration_ms,
                "repo_digest": self.repo_digest,
                "tree_digest": self.tree_digest,
                "created_at": self.created_at,
                "created_stamp": stamp(self.created_at),
                "passed": self.passed,
                "digest": self.digest,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationReport:
        return cls(
            run_id=data.get("run_id", ""),
            gates=[GateResult.from_dict(g) for g in data.get("gates", [])],
            duration_ms=int(data.get("duration_ms", 0)),
            repo_digest=data.get("repo_digest", ""),
            tree_digest=data.get("tree_digest", ""),
            created_at=float(data.get("created_at", now())),
        )


@dataclass
class Run:
    """One autonomous engineering attempt."""

    run_id: str
    goal: str
    status: RunStatus = RunStatus.RUNNING
    steps: list[Step] = field(default_factory=list)
    started_at: float = field(default_factory=now)
    ended_at: float | None = None
    max_steps: int = 32
    planner: str = "unknown"
    outcome: str = ""
    verification: VerificationReport | None = None
    #: Repository-relative paths the run's skills reported touching, in order.
    touched: list[str] = field(default_factory=list)

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def denied_steps(self) -> list[Step]:
        return [s for s in self.steps if s.denied]

    def add_step(self, invocation: Invocation) -> Step:
        step = Step(index=len(self.steps), invocation=invocation)
        self.steps.append(step)
        return step

    def record_artifacts(self, artifacts: Iterable[str]) -> None:
        """Append newly seen artifact paths, preserving first-touch order."""
        for artifact in artifacts:
            if artifact and artifact not in self.touched:
                self.touched.append(artifact)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(
            {
                "run_id": self.run_id,
                "goal": self.goal,
                "status": self.status,
                "planner": self.planner,
                "max_steps": self.max_steps,
                "started_at": self.started_at,
                "started_stamp": stamp(self.started_at),
                "ended_at": self.ended_at,
                "outcome": self.outcome,
                "steps": self.steps,
                "verification": self.verification,
                "touched": self.touched,
            }
        )

    @classmethod
    def new(cls, goal: str, *, max_steps: int = 32, planner: str = "unknown") -> Run:
        return cls(run_id=new_id("run"), goal=goal, max_steps=max_steps, planner=planner)
