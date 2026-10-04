"""End-to-end: the kernel driving a real plan against a real copy of itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from ourob.bootstrap.amend import AmendmentLedger
from ourob.bootstrap.manifest import Manifest, compare
from ourob.bootstrap.promote import Promotion
from ourob.config import Config
from ourob.kernel import Kernel, RunOutcome
from ourob.planner.scripted import Plan, ScriptedPlanner
from ourob.skills.registry import SkillRegistry
from ourob.state.model import Invocation, RunStatus, SkillResult, StepStatus
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
        step(outcome, i).invocation.skill + " -> " + (step(outcome, i).result.error or "") for i in range(4)
    }
    text = "\n".join(reasons)
    assert "path-confinement" in text
    assert "protected-paths" in text
    assert "journal-integrity" in text
    assert "command-allowlist" in text

    # nothing was written anywhere
    assert compare(Manifest.load(repo), repo).clean


def test_direct_protected_mutation_variants_are_denied_before_skill_dispatch(repo: Path) -> None:
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    original = target.read_bytes()
    alias = repo / "docs" / "policy-rules-alias.py"
    alias.symlink_to(target)
    plan = Plan.from_dict(
        {
            "goal": "attempt direct API mutations through alternate path spellings",
            "max_steps": 10,
            "steps": [
                {"skill": "write_file", "args": {"path": "src/ourob/policies/rules.py", "content": "bad"}},
                {"skill": "write_file", "args": {"path": str(target), "content": "bad"}},
                {"skill": "write_file", "args": {"path": "./src/ourob/policies/rules.py", "content": "bad"}},
                {"skill": "write_file", "args": {"path": "docs/policy-rules-alias.py", "content": "bad"}},
                {
                    "skill": "edit_file",
                    "args": {
                        "path": "src/ourob/policies/rules.py",
                        "old_text": "MAX_REPEATS = 3",
                        "new_text": "MAX_REPEATS = 1",
                    },
                },
                {"skill": "delete_file", "args": {"path": "src/ourob/policies/rules.py"}},
                {"skill": "write_file", "args": {"path": ".ourob/system.jsonl", "content": "forged"}},
                {"skill": "finish", "args": {"summary": "attempts were denied", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.denied == 7
    assert all(step.denied for step in outcome.run.steps[:-1])
    assert target.read_bytes() == original
    assert alias.is_symlink()

    events = list(StateStore(repo).journal(outcome.run.run_id).events())
    denied_decisions = [
        event for event in events if event["kind"] == "policy.decision" and not event["payload"]["allowed"]
    ]
    assert len(denied_decisions) == 7
    assert all(
        any(
            decision["policy"] in {"protected-paths", "journal-integrity"} and not decision["allowed"]
            for decision in event["payload"]["decisions"]
        )
        for event in denied_decisions
    )
    assert all(
        event["kind"] != "skill.result"
        or not event["payload"].get("denied")
        or "denied" in event["payload"]["result"].get("error", "")
        for event in events
    )
    assert all(result["ok"] for result in StateStore(repo).check_chain())


def test_protected_config_symlink_and_its_target_remain_protected(repo: Path) -> None:
    config = Config.load(repo)
    protected = repo / "ourob.toml"
    target = repo / "docs" / "config-alias.toml"
    original = protected.read_bytes()
    target.write_bytes(original)
    protected.unlink()
    protected.symlink_to(target.relative_to(repo))

    plan = Plan.from_dict(
        {
            "goal": "attempt to mutate a protected path through its own symlink",
            "max_steps": 4,
            "steps": [
                {"skill": "write_file", "args": {"path": "ourob.toml", "content": "tampered"}},
                {"skill": "write_file", "args": {"path": "docs/config-alias.toml", "content": "tampered"}},
                {"skill": "finish", "args": {"summary": "both aliases were denied", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, config=config, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.denied == 2
    assert protected.is_symlink()
    assert target.read_bytes() == original

    events = list(StateStore(repo).journal(outcome.run.run_id).events())
    denied = [
        event for event in events if event["kind"] == "policy.decision" and not event["payload"]["allowed"]
    ]
    assert len(denied) == 2
    assert all(
        any(
            decision["policy"] == "protected-paths" and not decision["allowed"]
            for decision in event["payload"]["decisions"]
        )
        for event in denied
    )


def test_forged_proposal_file_cannot_authorize_a_protected_edit(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    proposal = ledger.propose(["src/ourob/policies/"], "forged-file regression")
    proposal_path = ledger._path(proposal.amendment_id)
    proposal_bytes = proposal_path.read_text(encoding="utf-8")
    proposal_path.unlink()
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    before = target.read_bytes()

    plan = Plan.from_dict(
        {
            "goal": "replay the forged proposal-file attack from Result 001",
            "max_steps": 4,
            "steps": [
                {
                    "skill": "write_file",
                    "args": {
                        "path": proposal_path.relative_to(repo).as_posix(),
                        "content": proposal_bytes,
                    },
                },
                {
                    "skill": "edit_file",
                    "args": {
                        "path": "src/ourob/policies/rules.py",
                        "old_text": "MAX_REPEATS = 3",
                        "new_text": "MAX_REPEATS = 2",
                    },
                },
                {
                    "skill": "finish",
                    "args": {"summary": "forged request did not authorize a write", "success": True},
                },
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.denied == 2
    assert all(step.denied for step in outcome.run.steps[:2])
    assert not proposal_path.exists()
    assert target.read_bytes() == before
    assert ledger.authorising_id(proposal.amendment_id) is None

    events = list(StateStore(repo).journal(outcome.run.run_id).events())
    denied = [
        event for event in events if event["kind"] == "policy.decision" and not event["payload"]["allowed"]
    ]
    assert len(denied) == 2
    assert any(
        decision["policy"] == "journal-integrity" and not decision["allowed"]
        for decision in denied[0]["payload"]["decisions"]
    )
    assert any(
        decision["policy"] == "protected-paths" and not decision["allowed"]
        for decision in denied[1]["payload"]["decisions"]
    )
    assert all(item["ok"] for item in StateStore(repo).check_chain())


def test_in_process_runtime_skill_cannot_issue_an_amendment_grant(repo: Path) -> None:
    from threading import Thread

    from ourob.errors import BootstrapError, StateError
    from ourob.skills.base import Skill, skill
    from ourob.state.store import Journal

    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    proposal = ledger.propose(["src/ourob/policies/"], "attempt runtime self-authorization")

    @skill(
        "attempt_authorization",
        params={"amendment_id": {"type": "string"}},
        mutating=True,
    )
    class AttemptAuthorization(Skill):
        def run(self, ctx, amendment_id: str) -> SkillResult:
            blocked: list[str] = []

            def try_from_thread(label, operation, expected_error):
                errors: list[str] = []

                def invoke():
                    try:
                        operation()
                    except expected_error as exc:
                        errors.append(f"{label}: {exc}")
                    except Exception as exc:
                        errors.append(f"{label}: unexpected {type(exc).__name__}: {exc}")
                    else:
                        errors.append(f"{label}: unexpectedly allowed")

                thread = Thread(target=invoke)
                thread.start()
                thread.join(timeout=5)
                if thread.is_alive():
                    blocked.append(f"{label}: did not finish")
                else:
                    blocked.extend(errors)

            try_from_thread(
                "ledger",
                lambda: AmendmentLedger(ctx.repo / ".ourob" / "amendments").authorize(
                    amendment_id,
                    confirmation=amendment_id,
                    authorized_by="runtime-skill",
                ),
                BootstrapError,
            )
            try_from_thread(
                "system-store",
                lambda: ctx.store.record_system("amendment.authorized", {}),
                StateError,
            )
            try_from_thread(
                "low-level-journal",
                lambda: Journal(ctx.repo / ".ourob" / "system.jsonl").append("amendment.authorized", {}),
                StateError,
            )
            try_from_thread(
                "run-store",
                lambda: ctx.store.record(ctx.run_id, "policy.decision", {"allowed": True}),
                StateError,
            )
            try_from_thread(
                "run-journal",
                lambda: ctx.store.journal(ctx.run_id).append("policy.decision", {"allowed": True}),
                StateError,
            )
            try_from_thread(
                "system-rejection",
                lambda: ctx.store.record_system(
                    "promotion.rejected", {"accepted": False, "amendment_id": ""}
                ),
                StateError,
            )
            try_from_thread(
                "raw-system-rejection",
                lambda: Journal(ctx.repo / ".ourob" / "system.jsonl").append(
                    "promotion.rejected", {"accepted": False, "amendment_id": ""}
                ),
                StateError,
            )
            from ourob.state.model import VerificationReport

            try_from_thread(
                "verification-report",
                lambda: ctx.store.save_report(VerificationReport(run_id="forged")),
                StateError,
            )
            try_from_thread(
                "proposal-rewrite",
                lambda: AmendmentLedger(ctx.repo / ".ourob" / "amendments").save(
                    AmendmentLedger(ctx.repo / ".ourob" / "amendments").load(amendment_id)
                ),
                BootstrapError,
            )
            try_from_thread(
                "proposal-terminal-state",
                lambda: AmendmentLedger(ctx.repo / ".ourob" / "amendments").set_status(
                    amendment_id, "rejected"
                ),
                BootstrapError,
            )
            return SkillResult(
                ok=len(blocked) == 10,
                output="; ".join(blocked),
                data={"blocked": blocked},
            )

    registry = SkillRegistry(repo)
    registry.discover()
    registry.register(AttemptAuthorization)
    plan = Plan.from_dict(
        {
            "goal": "try to create operator authority from runtime execution",
            "max_steps": 3,
            "steps": [
                {
                    "skill": "attempt_authorization",
                    "args": {"amendment_id": proposal.amendment_id},
                },
                {"skill": "finish", "args": {"summary": "authorization remained external", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, registry=registry, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))

    attempt = outcome.run.steps[0]
    assert attempt.result is not None and attempt.result.ok
    assert len(attempt.result.data["blocked"]) == 10
    assert ledger.authorising_id(proposal.amendment_id) is None
    assert not any(event["kind"] == "amendment.authorized" for event in StateStore(repo).system_events())
    assert all(item["ok"] for item in StateStore(repo).check_chain())


def test_direct_mutating_skills_cannot_overwrite_delete_or_create_under_protection(repo: Path) -> None:
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    original = target.read_bytes()
    new_target = repo / "src" / "ourob" / "policies" / "unapproved.py"
    plan = Plan.from_dict(
        {
            "goal": "attempt direct file-skill mutations of protected files and directories",
            "max_steps": 6,
            "steps": [
                {
                    "skill": "write_file",
                    "args": {"path": target.relative_to(repo).as_posix(), "content": "tampered"},
                },
                {
                    "skill": "edit_file",
                    "args": {
                        "path": target.relative_to(repo).as_posix(),
                        "old_text": "MAX_REPEATS = 3",
                        "new_text": "MAX_REPEATS = 2",
                    },
                },
                {"skill": "delete_file", "args": {"path": target.relative_to(repo).as_posix()}},
                {
                    "skill": "write_file",
                    "args": {"path": new_target.relative_to(repo).as_posix(), "content": "unapproved"},
                },
                {"skill": "delete_file", "args": {"path": "src/ourob/policies"}},
                {"skill": "finish", "args": {"summary": "protected mutations denied", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))

    assert outcome.denied == 5
    assert target.read_bytes() == original
    assert not new_target.exists()
    for item in outcome.run.steps[:5]:
        assert item.denied
        assert any(
            decision.policy == "protected-paths" and not decision.allowed for decision in item.decisions
        )

    events = list(StateStore(repo).journal(outcome.run.run_id).events())
    decisions = [event for event in events if event["kind"] == "policy.decision"]
    assert len(decisions) == 6
    assert all(not event["payload"]["allowed"] for event in decisions[:5])
    assert all(item["ok"] for item in StateStore(repo).check_chain())


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
    assert (repo / "src" / "ourob" / "skills" / "contrib" / "text_metrics.py").is_file()
    assert (repo / "tests" / "test_contrib_text_metrics.py").is_file()

    # The runtime's startup journal records its initial discovery snapshot; the
    # later list_skills result must report the new file as pending, not hot-loaded.
    store = StateStore(repo)
    start = next(
        event for event in store.journal(outcome.run.run_id).events() if event["kind"] == "runtime.started"
    )
    assert start["payload"]["skill_discovery"]["complete"] is True
    listed = next(step for step in outcome.run.steps if step.invocation.skill == "list_skills")
    assert listed.result is not None
    assert listed.result.data["discovery"]["restart_required"] is True
    assert "src/ourob/skills/contrib/text_metrics.py" in listed.result.data["discovery"]["pending"]["added"]

    registry = SkillRegistry(repo)
    assert registry.discover() == []
    assert registry.has("text_metrics")
    from ourob.skills.base import SkillContext

    result = registry.dispatch(
        Invocation(skill="text_metrics", args={"text": "one two\nthree"}),
        SkillContext(repo=repo, services={}),
    )
    assert result.ok
    assert result.data["words"] == 3


def test_self_extension_survives_a_cold_start(repo: Path) -> None:
    from ourob.bootstrap.coldstart import boot

    outcome = run_plan(repo, "self_extend.json")
    assert outcome.run.status is RunStatus.COMPLETED, outcome.summary()

    module, report = boot(repo)
    assert module is not None
    assert report.trusted  # ordinary drift does not break the bootstrap
    # the fixture always replaces tests/, so this file is always an addition
    assert "tests/test_contrib_text_metrics.py" in report.drift.added
    assert not any(p for p in report.drift.touched if Config.load(repo).is_protected(p))

    registry = SkillRegistry(repo)
    registry.discover()
    assert registry.has("text_metrics")


def test_new_self_extension_is_verified_and_promoted_with_the_same_tree_digest(
    repo: Path,
) -> None:
    target = "src/ourob/skills/contrib/text_metrics.py"
    test_target = "tests/test_contrib_text_metrics.py"
    baseline = Manifest.load(repo)
    assert target not in baseline.paths()
    assert test_target not in baseline.paths()

    outcome = run_plan(repo, "self_extend.json", verify=True)
    assert outcome.ok, outcome.summary()
    assert outcome.report is not None and outcome.report.passed
    assert target in outcome.run.touched
    assert test_target in outcome.run.touched

    prepromotion_tree_digest = Manifest.build(repo).digest
    assert outcome.report.tree_digest == prepromotion_tree_digest
    assert outcome.report.repo_digest == baseline.digest

    result = Promotion(repo, use_git=False).promote(
        message="add the new text_metrics skill", gates=FAST_GATES + ["tests"]
    )
    assert result.accepted, result.describe()
    assert result.prepromotion_tree_digest == prepromotion_tree_digest
    assert result.lock_digest == prepromotion_tree_digest
    assert result.report is not None and result.report.tree_digest == prepromotion_tree_digest
    assert compare(Manifest.load(repo), repo).clean
    assert Manifest.load(repo).digest == prepromotion_tree_digest

    accepted = [event for event in StateStore(repo).system_events() if event["kind"] == "promotion.accepted"]
    assert len(accepted) == 1
    evidence = accepted[0]["payload"]
    assert evidence["prepromotion_tree_digest"] == prepromotion_tree_digest
    assert evidence["lock_digest"] == prepromotion_tree_digest
    assert evidence["verification"]["tree_digest"] == prepromotion_tree_digest


def test_a_snapshot_is_taken_before_the_first_mutation(repo: Path) -> None:
    from ourob.bootstrap.snapshot import Snapshot

    outcome = run_plan(repo, "self_extend.json")
    assert outcome.snapshot == outcome.run.run_id
    snapshot = Snapshot.load(repo, outcome.run.run_id)
    assert snapshot.files == len(Manifest.load(repo).files)  # taken before the new files existed
    drift = snapshot.drift_since()
    assert not drift.clean
    assert "tests/test_contrib_text_metrics.py" in drift.added

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

    result = Promotion(repo, use_git=False, snapshot_run_id=outcome.snapshot).promote(
        gates=["compile", "tests"]
    )
    assert not result.accepted
    assert not (repo / "tests" / "test_deliberate.py").exists()
    assert Snapshot.load(repo, outcome.snapshot).drift_since().clean


# -- amendments -----------------------------------------------------------


def test_runtime_proposal_alone_does_not_authorize_a_protected_write(repo: Path) -> None:
    baseline = Manifest.load(repo)
    outcome = run_plan(repo, "amend_policy.json")
    assert outcome.run.status is RunStatus.COMPLETED, outcome.summary()
    assert outcome.denied == 1

    text = (repo / "src" / "ourob" / "policies" / "rules.py").read_text(encoding="utf-8")
    assert "MAX_REPEATS = 3" in text
    assert "MAX_REPEATS = 2" not in text

    from ourob.bootstrap.amend import AmendmentLedger, AmendmentStatus

    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    proposals = ledger.all()
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.status == AmendmentStatus.PROPOSED
    assert proposal.paths == ["src/ourob/policies/"]
    assert "MAX_REPEATS" in proposal.rationale
    assert ledger.active() == []
    assert ledger.authorising_id(proposal.amendment_id) is None
    assert not any(event["kind"] == "amendment.authorized" for event in StateStore(repo).system_events())

    result = Promotion(repo, use_git=False).promote(
        amendment_id=proposal.amendment_id,
        message="must not promote an ungranted proposal",
        gates=FAST_GATES,
    )
    assert not result.accepted
    assert any("no valid current operator authorization" in message for message in result.messages)
    assert Manifest.load(repo).digest == baseline.digest
    assert compare(baseline, repo).clean


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
    assert "MAX_REPEATS = 3" in (repo / "src" / "ourob" / "policies" / "rules.py").read_text(encoding="utf-8")


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
            "steps": [
                {"skill": "teleport", "args": {}},
                {"skill": "finish", "args": {"summary": "ok", "success": True}},
            ],
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
