"""Policy contracts.

A policy answers one question before an invocation happens: *is this allowed?*
Policies are deliberately dumb and local -- each one looks at a single property
of the call -- and the :class:`PolicySet` combines them with deny-wins
semantics.  Every verdict, including the allows, is written to the journal, so
after the fact you can see not just what the runtime did but what it was
permitted to do and by which rule.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import PolicyViolation
from ..state.model import Invocation, PolicyDecision

#: Argument keys that carry a filesystem path and therefore get confined.
PATH_KEYS = frozenset({"path", "file", "target", "directory", "dir", "destination"})


@dataclass(frozen=True)
class Verdict:
    policy: str
    allowed: bool
    reason: str
    severity: str = "info"

    def to_decision(self) -> PolicyDecision:
        return PolicyDecision(
            policy=self.policy, allowed=self.allowed, reason=self.reason, severity=self.severity
        )

    @classmethod
    def allow(cls, policy: str, reason: str = "ok") -> Verdict:
        return cls(policy=policy, allowed=True, reason=reason)

    @classmethod
    def deny(cls, policy: str, reason: str, *, severity: str = "block") -> Verdict:
        return cls(policy=policy, allowed=False, reason=reason, severity=severity)


@dataclass
class ReviewContext:
    """Everything a policy is allowed to see when judging one invocation."""

    repo: Path
    config: Config
    invocation: Invocation
    step_index: int = 0
    budget_used: int = 0
    budget_limit: int = 32
    services: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def skill(self) -> str:
        return self.invocation.skill

    @property
    def args(self) -> dict[str, Any]:
        return self.invocation.args or {}

    def path_args(self) -> list[tuple[str, str]]:
        """``(key, value)`` for every argument that names a path."""
        return [(k, str(v)) for k, v in self.args.items() if k in PATH_KEYS and isinstance(v, str)]

    def ledger(self) -> Any | None:
        return self.services.get("ledger")

    def amendment_for(self, relpath: str) -> Any | None:
        ledger = self.ledger()
        if ledger is None:
            return None
        return ledger.authorising(relpath)

    def amended_paths(self) -> list[str]:
        return list(self.services.get("amended_paths") or [])

    def history_key(self) -> str:
        import json

        return json.dumps(
            {"skill": self.invocation.skill, "args": self.args}, sort_keys=True, default=str
        )

    def recent_failures(self) -> int:
        """How many consecutive identical invocations at the tail just failed."""
        key = self.history_key()
        count = 0
        for entry in reversed(self.history):
            if entry.get("key") != key:
                break
            if entry.get("ok") is False:
                count += 1
            else:
                break
        return count


class Policy(ABC):
    """One rule.  ``review`` must never raise."""

    name: str = "policy"
    title: str = "Policy"
    description: str = ""
    blocking: bool = True
    #: When true the policy is consulted for read-only skills too.
    applies_to_reads: bool = True

    @abstractmethod
    def review(self, ctx: ReviewContext) -> Verdict:
        """Return a verdict for this invocation."""

    def decision(self, ctx: ReviewContext) -> PolicyDecision:
        try:
            verdict = self.review(ctx)
        except Exception as exc:  # a crashing policy fails closed
            verdict = Verdict.deny(self.name, f"policy raised {type(exc).__name__}: {exc}")
        decision = verdict.to_decision()
        if not self.blocking and not decision.allowed:
            decision.severity = "warn"
        return decision


@dataclass
class ReviewOutcome:
    allowed: bool
    decisions: list[PolicyDecision] = field(default_factory=list)

    @property
    def denials(self) -> list[PolicyDecision]:
        return [d for d in self.decisions if not d.allowed and d.severity == "block"]

    @property
    def warnings(self) -> list[PolicyDecision]:
        return [d for d in self.decisions if not d.allowed and d.severity != "block"]

    def reason(self) -> str:
        if self.allowed:
            return "; ".join(d.reason for d in self.warnings) or "permitted"
        return "; ".join(f"{d.policy}: {d.reason}" for d in self.denials)

    def raise_if_denied(self) -> None:
        if not self.allowed:
            first = self.denials[0] if self.denials else None
            raise PolicyViolation(
                self.reason(), policy=first.policy if first else "unknown"
            )


class PolicySet:
    """An ordered collection of policies with deny-wins evaluation."""

    def __init__(self, policies: Iterable[Policy]) -> None:
        self.policies = list(policies)

    def names(self) -> list[str]:
        return [p.name for p in self.policies]

    def review(self, ctx: ReviewContext) -> ReviewOutcome:
        decisions: list[PolicyDecision] = []
        for policy in self.policies:
            decisions.append(policy.decision(ctx))
        return ReviewOutcome(
            allowed=all(d.allowed or d.severity != "block" for d in decisions),
            decisions=decisions,
        )

    def catalogue(self) -> list[dict[str, Any]]:
        return [
            {
                "name": p.name,
                "title": p.title,
                "description": p.description,
                "blocking": p.blocking,
            }
            for p in self.policies
        ]
