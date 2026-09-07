"""End-to-end: the kernel driving a real plan against a real copy of itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from ourob.bootstrap.manifest import Manifest, compare
from ourob.bootstrap.promote import Promotion
from ourob.config import Config
from ourob.kernel import Kernel, RunOutcome
from ourob.planner.scripted import Plan, ScriptedPlanner
from ourob.skills.registry import SkillRegistry
from ourob.state.model import Invocation, RunStatus, StepStatus
from ourob.state.store import StateStore

FAST_GATES = ["compile", "import", "manifest", "policy-integrity", "skill-contract"]


def run_plan(repo: Path, plan_name: str, *, verify: bool = False, max_steps: int = 0) -> RunOutcome:
    plan = Plan.load(repo / "plans" / plan_name)
    kernel = Kernel(repo, verify_at_end=verify)
    return kernel.run(plan.goal, ScriptedPlanner(plan), max_steps=max_steps or plan.max_steps)


def step(outcome: RunOutcome, index: int):
    return outcome.run.steps[index]


# -- a quiet run ----------------------------------------------------------


def test_a_read_only_plan_completes(repo: Path) -> None:
    outcome = run_plan(repo, "healthcheck.json")
    assert outcome.run.status is RunStatus.COMPLETED
    assert outcome.denied == 0
    assert outcome.failed == 0
    assert outcome.run.step_count == 5
    assert step(outcome, 0).invocation.skill == "read_manifest"


def test_every_step_is_journalled_with_its_verdicts(repo: Path) -> None:
    outcome = run_plan(repo, "healthcheck.json")
    store = StateStore(repo)
    events = list(store.journal(outcome.run.run_id).events())
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run.started"
    assert kinds.count("step.planned") == 5
    assert kinds.count("policy.decision") == 5
    assert kinds.count("skill.result") == 5
    assert kinds[-1] == "run.finished"

    replayed = store.replay(outcome.run.run_id)
    assert replayed.step_count == 5
    assert replayed.steps[0].decisions, "every step must carry the verdicts that allowed it"
    assert all(d.allowed for s in replayed.steps for d in s.decisions)


def test_the_journal_stays_intact_across_a_run(repo: Path) -> None:
    outcome = run_plan(repo, "healthcheck.json")
    store = StateStore(repo)
    ok, detail = store.journal(outcome.run.run_id).check_chain()
    assert ok, detail
    assert all(r["ok"] for r in store.check_chain()), store.check_chain()


def test_a_run_records_what_it_was_made_of(repo: Path) -> None:
    outcome = run_plan(repo, "healthcheck.json")
    events = list(StateStore(repo).journal(outcome.run.run_id).events())
    started = next(e for e in events if e["kind"] == "runtime.started")
    payload = started["payload"]
    assert "read_file" in payload["skills"]
    assert "protected-paths" in payload["policies"]
    assert payload["lock_digest"] == Manifest.load(repo).digest


# -- refusals -------------------------------------------------------------


def test_the_runtime_refuses_everything_it_should(repo: Path) -> None:
    outcome = run_plan(repo, "refused.json")
    assert outcome.denied == 4
    assert outcome.run.step_count == 5

    reasons = {
        step(outcome, i).invocation.skill
        + " -> "
        + (step(outcome, i).result.error or "")
        for i in range(4)
    }
    text = "\n".join(reasons)
    assert "path-confinement" in text
    assert "protected-paths" in text
    assert "journal-integrity" in text
    assert "command-allowlist" in text

    # nothing was written anywhere
    assert compare(Manifest.load(repo), repo).clean


def test_a_denied_step_does_not_execute_the_skill(repo: Path) -> None:
    outcome = run_plan(repo, "refused.json")
    first = step(outcome, 0)
    assert first.status is StepStatus.DENIED
    assert first.result is not None
    assert "denied by policy" in (first.result.error or "")
    assert not (repo.parent / "etc" / "ourob-escaped.txt").exists()


def test_the_run_continues_after_a_refusal(repo: Path) -> None:
    outcome = run_plan(repo, "refused.json")
    assert outcome.run.status is RunStatus.COMPLETED
    assert step(outcome, 4).invocation.skill == "finish"
    assert step(outcome, 4).status is StepStatus.OK


# -- self-extension -------------------------------------------------------


def test_the_runtime_adds_a_skill_to_itself(repo: Path) -> None:
    outcome = run_plan(repo, "self_extend.json")
    assert outcome.run.status is RunStatus.COMPLETED, outcome.summary()
    assert (repo / "src" / "ourob" / "skills" / "contrib" / "rot13.py").is_file()
    assert (repo / "tests" / "test_contrib_rot13.py").is_file()

    # a fresh registry -- no restart, no install step -- sees it
    registry = SkillRegistry(repo)
    assert registry.discover() == []
    assert registry.has("rot13")
    from ourob.skills.base import SkillContext

    result = registry.dispatch(
        Invocation(skill="rot13", args={"text": "ourob"}), SkillContext(repo=repo, services={})
    )
    assert result.ok
    assert result.output == "bhebo"


def test_self_extension_survives_a_cold_start(repo: Path) -> None:
    from ourob.bootstrap.coldstart import boot

    outcome = run_plan(repo, "self_extend.json")
    assert outcome.run.status is RunStatus.COMPLETED, outcome.summary()

    module, report = boot(repo)
    assert module is not None
    assert report.trusted  # ordinary drift does not break the bootstrap
    # the fixture always replaces tests/, so this file is always an addition
    assert "tests/test_contrib_rot13.py" in report.drift.added
    assert not any(p for p in report.drift.touched if Config.load(repo).is_protected(p))

    registry = SkillRegistry(repo)
    registry.discover()
    assert registry.has("rot13")


def test_a_verified_run_can_be_promoted(repo: Path) -> None:
    outcome = run_plan(repo, "self_extend.json", verify=True)
    assert outcome.ok, outcome.summary()
    assert outcome.report is not None and outcome.report.passed

    result = Promotion(repo, use_git=False).promote(
        message="add the rot13 skill", gates=FAST_GATES + ["tests"]
    )
    assert result.accepted, result.describe()
    assert compare(Manifest.load(repo), repo).clean
    assert "src/ourob/skills/contrib/rot13.py" in Manifest.load(repo).paths()


def test_a_snapshot_is_taken_before_the_first_mutation(repo: Path) -> None:
    from ourob.bootstrap.snapshot import Snapshot

    outcome = run_plan(repo, "self_extend.json")
    assert outcome.snapshot == outcome.run.run_id
    snapshot = Snapshot.load(repo, outcome.run.run_id)
    assert snapshot.files == len(Manifest.load(repo).files)  # taken before the new files existed
    drift = snapshot.drift_since()
    assert not drift.clean
    assert "tests/test_contrib_rot13.py" in drift.added

    # rolling back returns the tree to exactly the pre-run state
    snapshot.restore()
    assert snapshot.drift_since().clean


def test_a_read_only_run_takes_no_snapshot(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "look but do not touch",
            "max_steps": 4,
            "steps": [
                {"skill": "read_file", "args": {"path": "README.md"}},
                {"skill": "list_dir", "args": {"path": "src"}},
                {"skill": "finish", "args": {"summary": "looked", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.snapshot == ""
    assert compare(Manifest.load(repo), repo).clean


def test_a_failed_change_can_be_rolled_back_exactly(repo: Path) -> None:
    from ourob.bootstrap.snapshot import Snapshot

    plan = Plan.from_dict(
        {
            "goal": "break the build on purpose",
            "max_steps": 4,
            "steps": [
                {
                    "skill": "write_file",
                    "args": {
                        "path": "tests/test_deliberate.py",
                        "content": "def test_nope():\n    assert False\n",
                    },
                },
                {"skill": "finish", "args": {"summary": "done", "success": True}},
            ],
        }
    )
    kernel = Kernel(repo, verify_at_end=True)
    outcome = kernel.run(plan.goal, ScriptedPlanner(plan))

    assert outcome.run.status is RunStatus.FAILED
    assert outcome.report is not None and not outcome.report.passed
    assert "tests" in [g.gate for g in outcome.report.failures]

    result = Promotion(
        repo, use_git=False, snapshot_run_id=outcome.snapshot
    ).promote(gates=["compile", "tests"])
    assert not result.accepted
    assert not (repo / "tests" / "test_deliberate.py").exists()
    assert Snapshot.load(repo, outcome.snapshot).drift_since().clean


# -- amendments -----------------------------------------------------------


def test_the_runtime_amends_its_own_guardrails_under_the_rules(repo: Path) -> None:
    outcome = run_plan(repo, "amend_policy.json")
    assert outcome.run.status is RunStatus.COMPLETED, outcome.summary()
    assert outcome.denied == 0

    text = (repo / "src" / "ourob" / "policies" / "rules.py").read_text(encoding="utf-8")
    assert "MAX_REPEATS = 2" in text

    from ourob.bootstrap.amend import AmendmentLedger, AmendmentStatus

    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendments = ledger.active()
    assert len(amendments) == 1
    assert amendments[0].paths == ["src/ourob/policies/"]
    assert "MAX_REPEATS" in amendments[0].rationale

    # the change is not official until it is promoted under the amendment
    result = Promotion(repo, use_git=False).promote(
        amendment_id=amendments[0].amendment_id,
        message="lower MAX_REPEATS",
        gates=FAST_GATES + ["tests"],
    )
    assert result.accepted, result.describe()
    assert ledger.load(amendments[0].amendment_id).status == AmendmentStatus.RATIFIED
    assert compare(Manifest.load(repo), repo).clean


def test_protected_writes_without_an_amendment_are_refused(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "edit a protected path without asking",
            "max_steps": 3,
            "steps": [
                {
                    "skill": "edit_file",
                    "args": {
                        "path": "src/ourob/policies/rules.py",
                        "old_text": "MAX_REPEATS = 3",
                        "new_text": "MAX_REPEATS = 99",
                    },
                },
                {"skill": "finish", "args": {"summary": "should not get here", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.denied == 1
    assert "MAX_REPEATS = 3" in (repo / "src" / "ourob" / "policies" / "rules.py").read_text(
        encoding="utf-8"
    )


# -- limits ---------------------------------------------------------------


def test_the_budget_stops_a_runaway_plan(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "loop forever",
            "max_steps": 3,
            "steps": [{"skill": "read_file", "args": {"path": "README.md"}, "repeat": 50}],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(
        plan.goal, ScriptedPlanner(plan), max_steps=plan.max_steps
    )
    assert outcome.run.status is RunStatus.BLOCKED
    assert outcome.run.step_count == 3
    assert "budget" in outcome.outcome


def test_the_loop_breaker_stops_a_repeated_failure(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "retry the same failing call",
            "max_steps": 8,
            "steps": [
                {"skill": "read_file", "args": {"path": "definitely-missing.py"}, "repeat": 8},
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    denials = [s for s in outcome.run.steps if s.status is StepStatus.DENIED]
    executed = [s for s in outcome.run.steps if s.status is not StepStatus.DENIED]
    assert denials, "the loop breaker must eventually refuse the repeated call"
    assert any("loop-breaker" in d.policy for s in denials for d in s.decisions)
    # the skill actually ran three times; every attempt after that was refused
    assert len(executed) == 3
    assert len(denials) == 5


def test_a_broken_planner_is_recorded_not_raised(repo: Path) -> None:
    from ourob.planner.base import Planner

    class Broken(Planner):
        name = "broken"

        def next_action(self, view):
            raise RuntimeError("planner exploded")

    outcome = Kernel(repo, verify_at_end=False).run("explode", Broken())
    assert outcome.run.step_count == 0
    assert "planner exploded" in outcome.run.outcome
    events = list(StateStore(repo).journal(outcome.run.run_id).events())
    assert any(e["kind"] == "planner.error" for e in events)


def test_an_unknown_skill_is_a_recorded_error_not_a_crash(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "ask for something that does not exist",
            "max_steps": 3,
            "steps": [{"skill": "teleport", "args": {}},
                      {"skill": "finish", "args": {"summary": "ok", "success": True}}],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.failed == 1
    assert "no skill named" in (step(outcome, 0).result.error or "")


# -- verification at the end of a run -------------------------------------


def test_verification_runs_at_the_end_of_a_run(repo: Path) -> None:
    outcome = run_plan(repo, "healthcheck.json", verify=True)
    assert outcome.report is not None
    assert outcome.report.passed, outcome.summary()
    assert outcome.report.run_id == outcome.run.run_id
    assert (repo / ".ourob" / "verify" / f"{outcome.run.run_id}.json").is_file()


def test_a_clean_run_reports_its_outcome(repo: Path) -> None:
    outcome = run_plan(repo, "healthcheck.json", verify=True)
    summary = outcome.summary()
    assert "completed" in summary
    assert "Health check complete." in summary
    assert "verification PASSED" in summary


def test_outcome_ok_requires_green_gates(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "write a syntax error into the runtime",
            "max_steps": 3,
            "steps": [
                {
                    "skill": "write_file",
                    "args": {"path": "src/ourob/broken.py", "content": "def oops(:\n"},
                },
                {"skill": "finish", "args": {"summary": "done", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=True).run(plan.goal, ScriptedPlanner(plan))
    assert not outcome.ok
    assert outcome.run.status is RunStatus.FAILED
    assert "compile" in [g.gate for g in outcome.report.failures]


# -- plans ----------------------------------------------------------------


def test_a_plan_must_state_a_goal() -> None:
    from ourob.errors import ConfigError

    with pytest.raises(ConfigError):
        Plan.from_dict({"steps": [{"skill": "finish", "args": {}}]})


def test_a_plan_must_have_steps() -> None:
    from ourob.errors import ConfigError

    with pytest.raises(ConfigError):
        Plan.from_dict({"goal": "x", "steps": []})


def test_a_plan_step_must_name_a_skill() -> None:
    from ourob.errors import ConfigError

    with pytest.raises(ConfigError):
        Plan.from_dict({"goal": "x", "steps": [{"args": {}}]})


def test_plans_can_skip_a_step_after_a_failure() -> None:
    plan = Plan.from_dict(
        {
            "goal": "skip after failure",
            "max_steps": 4,
            "steps": [
                {"skill": "read_file", "args": {"path": "missing.py"}},
                {"skill": "list_dir", "args": {"path": "."}, "skip_if_failed": ["previous"]},
            ],
        }
    )
    planner = ScriptedPlanner(plan)
    from ourob.planner.base import Observation, RuntimeView

    def view_after_failure() -> RuntimeView:
        return RuntimeView(
            goal=plan.goal,
            run_id="r",
            step_index=1,
            budget_remaining=3,
            history=[Observation(index=0, skill="read_file", args={}, ok=False, error="nope")],
        )

    first = planner.next_action(view_after_failure())
    assert first is not None and first.skill == "read_file"
    # the second step declares skip_if_failed, and the previous step failed
    assert planner.next_action(view_after_failure()) is None


def test_shipped_plans_are_valid(repo: Path) -> None:
    for path in sorted((repo / "plans").glob("*.json")):
        plan = Plan.load(path)
        assert plan.goal
        assert plan.steps
        assert plan.max_steps >= plan.step_count
