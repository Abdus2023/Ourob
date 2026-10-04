"""The kernel: the loop that turns a goal into a verified change.

One iteration is: ask the planner, judge the answer with the policy set, write
both to the journal, execute the skill, write the result to the journal, hand a
compressed observation back to the planner.  Nothing else happens.

Two things the kernel does around the loop matter as much as the loop:

* it captures a **snapshot** before the first mutating step, so a failed
  promotion can be rolled back exactly;
* it runs the **verification suite** at the end, so a run never reports success
  on a tree that does not pass its own gates.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bootstrap.amend import AmendmentLedger, runtime_execution
from .bootstrap.manifest import Manifest, compare
from .bootstrap.snapshot import Snapshot
from .config import Config
from .planner.base import Observation, Planner, RuntimeView
from .policies.base import PolicySet, ReviewContext
from .skills.base import SkillContext
from .skills.registry import SkillRegistry
from .state.model import (
    Invocation,
    Run,
    RunStatus,
    SkillResult,
    Step,
    StepStatus,
    VerificationReport,
)
from .state.store import StateStore
from .verify.suite import VerificationSuite, format_report

TERMINAL_SKILL = "finish"


@dataclass
class RunOutcome:
    run: Run
    report: VerificationReport | None = None
    outcome: str = ""
    denied: int = 0
    failed: int = 0
    snapshot: str = ""
    touched: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.run.status is RunStatus.COMPLETED and (self.report is None or self.report.passed)

    def summary(self) -> str:
        lines = [
            f"run {self.run.run_id} -> {self.run.status.value}",
            f"  goal      {self.run.goal}",
            f"  steps     {self.run.step_count} ({self.denied} denied, {self.failed} errored)",
            f"  outcome   {self.outcome}",
        ]
        if self.snapshot:
            lines.append(f"  snapshot  {self.snapshot}")
        if self.touched:
            lines.append(f"  touched   {', '.join(self.touched)}")
        if self.report is not None:
            lines.append("  " + format_report(self.report).replace("\n", "\n  "))
        return "\n".join(lines)


@dataclass
class Kernel:
    repo: Path
    config: Config | None = None
    state: StateStore | None = None
    registry: SkillRegistry | None = None
    policies: PolicySet | None = None
    ledger: AmendmentLedger | None = None
    verify_at_end: bool = True
    snapshot_runs: bool = True
    logger: Callable[[str], None] | None = None
    observations: list[Observation] = field(default_factory=list)
    services: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()
        self.config = self.config or Config.load(self.repo)
        self.state = self.state or StateStore(self.repo)
        if self.registry is None:
            self.registry = SkillRegistry(self.repo)
        self.registry.discover()
        self.registry.freeze()
        if self.policies is None:
            from .policies.rules import default_policy_set

            self.policies = default_policy_set()
        self.ledger = self.ledger or AmendmentLedger(self.repo / ".ourob" / "amendments")
        # Authorization is not a runtime skill capability, even if a caller
        # accidentally supplied a ledger in its generic service dictionary.
        self.services.pop("ledger", None)

    # -- plumbing ---------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger(message)

    def _mutating_map(self) -> dict[str, bool]:
        return {name: self.registry.get(name).spec.mutating for name in self.registry.names()}

    def _param_map(self) -> dict[str, dict[str, Any]]:
        """Declared parameter schemas, so policies confine what skills say they carry."""
        return {name: dict(self.registry.get(name).spec.params) for name in self.registry.names()}

    def _skill_context(self, run_id: str, step: int) -> SkillContext:
        # The services dict carries the registry and config to skills. No
        # authorization service is exposed; proposal files never confer authority,
        # and the policy layer independently validates operator grants.
        self.services.setdefault("config", self.config)
        self.services.setdefault("registry", self.registry)
        return SkillContext(
            repo=self.repo,
            run_id=run_id,
            step=step,
            store=self.state,
            services=self.services,
            logger=self.logger,
        )

    def _view(self, run: Run) -> RuntimeView:
        return RuntimeView(
            goal=run.goal,
            run_id=run.run_id,
            step_index=run.step_count,
            budget_remaining=max(0, run.max_steps - run.step_count),
            skills=self.registry.catalogue(),
            skill_discovery=self.registry.discovery_status(),
            history=list(self.observations),
            policies=self.policies.catalogue(),
            protected_paths=self.config.protected_prefixes(),
        )

    # -- the loop ---------------------------------------------------------
    def run(self, goal: str, planner: Planner, *, max_steps: int | None = None) -> RunOutcome:
        with runtime_execution():
            return self._run_inside_runtime(goal, planner, max_steps=max_steps)

    def _run_inside_runtime(self, goal: str, planner: Planner, *, max_steps: int | None = None) -> RunOutcome:
        self.observations = []
        run = self.state.open_run(
            goal, max_steps=max_steps or self.config.policy.max_steps, planner=planner.name
        )
        self.state.record(
            run.run_id,
            "runtime.started",
            {
                "skills": self.registry.names(),
                "policies": self.policies.names(),
                "discovery_problems": self.registry.problems(),
                "skill_discovery": self.registry.discovery_status(),
                "lock_digest": self._lock_digest(),
            },
        )

        snapshot_id = ""
        outcome_message = ""
        terminal = False

        while run.step_count < run.max_steps:
            invocation = self._plan(planner, run)
            if invocation is None:
                outcome_message = run.outcome or outcome_message or "planner ended the run"
                break

            step = run.add_step(invocation)
            self.state.record(
                run.run_id, "step.planned", {"index": step.index, "invocation": invocation.to_dict()}
            )

            if not self._authorise(run, step, invocation):
                self._log(f"step {step.index}: {invocation.skill} DENIED -- {step.result.error}")
                if invocation.skill == TERMINAL_SKILL:
                    terminal = True
                    break
                continue

            if not snapshot_id and self.snapshot_runs and self._is_mutating(invocation):
                snapshot_id = self._capture_snapshot(run.run_id)
                self.services["snapshot_run_id"] = snapshot_id

            result = self.registry.dispatch(invocation, self._skill_context(run.run_id, step.index))
            step.result = result
            run.record_artifacts(result.artifacts)
            step.status = StepStatus.OK if result.ok else StepStatus.ERROR
            self.state.record(run.run_id, "skill.result", {"index": step.index, "result": result.to_dict()})
            self._log(
                f"step {step.index}: {invocation.skill} -> "
                f"{'ok' if result.ok else 'error'} ({result.duration_ms} ms)"
            )
            self._observe(step)

            if invocation.skill == TERMINAL_SKILL or result.data.get("terminal"):
                outcome_message = result.output or outcome_message
                terminal = True
                break

        if not terminal and run.step_count >= run.max_steps:
            outcome_message = f"step budget exhausted at {run.step_count} steps"
            run.status = RunStatus.BLOCKED
            run.outcome = outcome_message
        else:
            run.status = RunStatus.COMPLETED
            run.outcome = outcome_message or "no outcome recorded"

        report: VerificationReport | None = None
        if self.verify_at_end:
            report = self._verify(run)
            run.verification = report
            if not report.passed:
                run.status = RunStatus.FAILED
                run.outcome = f"{outcome_message}; verification failed: " + ", ".join(
                    g.gate for g in report.failures
                )

        outcome = RunOutcome(
            run=run,
            report=report,
            outcome=run.outcome,
            denied=len(run.denied_steps),
            failed=sum(1 for s in run.steps if s.result is not None and not s.result.ok),
            snapshot=snapshot_id,
            touched=list(run.touched),
        )
        self.state.close_run(run)
        return outcome

    # -- internals --------------------------------------------------------
    def _authorise(self, run: Run, step: Step, invocation: Invocation) -> bool:
        """Run the policy set over an invocation and journal the verdicts."""
        review = self.policies.review(
            ReviewContext(
                repo=self.repo,
                config=self.config,
                invocation=invocation,
                step_index=step.index,
                budget_used=step.index,
                budget_limit=run.max_steps,
                services={
                    "mutating_skills": self._mutating_map(),
                    "skill_params": self._param_map(),
                    "ledger": self.ledger,
                },
                history=[{**obs.to_dict(), "key": obs.key} for obs in self.observations],
            )
        )
        step.decisions = review.decisions
        self.state.record(
            run.run_id,
            "policy.decision",
            {
                "index": step.index,
                "allowed": review.allowed,
                "decisions": [d.to_dict() for d in review.decisions],
            },
        )
        if review.allowed:
            step.status = StepStatus.ALLOWED
            return True

        step.status = StepStatus.DENIED
        reason = review.reason()
        step.result = SkillResult(ok=False, output=reason, error=f"denied by policy: {reason}")
        self.state.record(
            run.run_id,
            "skill.result",
            {"index": step.index, "result": step.result.to_dict(), "denied": True},
        )
        self._observe(step, denied=True, denials=[d.policy for d in review.denials])
        return False

    def _plan(self, planner: Planner, run: Run) -> Invocation | None:
        try:
            return planner.next_action(self._view(run))
        except Exception as exc:
            self.state.record(run.run_id, "planner.error", {"error": f"{type(exc).__name__}: {exc}"})
            run.outcome = f"planner failed: {type(exc).__name__}: {exc}"
            return None

    def _is_mutating(self, invocation: Invocation) -> bool:
        if not self.registry.has(invocation.skill):
            return False
        return self.registry.get(invocation.skill).spec.mutating

    def _capture_snapshot(self, run_id: str) -> str:
        try:
            snapshot = Snapshot.capture(self.repo, run_id)
        except Exception as exc:
            self._log(f"snapshot failed: {exc}")
            return ""
        self.state.record(
            run_id,
            "snapshot.captured",
            {"run_id": run_id, "files": snapshot.files, "digest": snapshot.digest},
        )
        self._log(f"snapshot {run_id}: {snapshot.files} files")
        return run_id

    def _lock_digest(self) -> str:
        try:
            return Manifest.load(self.repo).digest
        except Exception:
            return ""

    def _observe(self, step: Step, *, denied: bool = False, denials: list[str] | None = None) -> Observation:
        result = step.result
        observation = Observation(
            index=step.index,
            skill=step.invocation.skill,
            args=dict(step.invocation.args),
            ok=bool(result.ok) if result is not None else False,
            output=(result.output if result is not None else "")[-4000:],
            error=result.error if result is not None else None,
            denied=denied,
            denials=denials or [],
        )
        self.observations.append(observation)
        return observation

    def _verify(self, run: Run) -> VerificationReport:
        suite = VerificationSuite(
            self.repo,
            self.config,
            state=self.state,
            run_id=run.run_id,
        )
        report = suite.run(save=True)
        self.state.record(
            run.run_id,
            "verification.report",
            {"passed": report.passed, "digest": report.digest, "summary": report.summary_line()},
        )
        with contextlib.suppress(Exception):
            self.state.record(
                run.run_id, "runtime.drift", compare(Manifest.load(self.repo), self.repo).to_dict()
            )
        return report
