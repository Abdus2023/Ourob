"""Every guardrail gets a direct test: the thing it allows and the thing it stops."""

from __future__ import annotations

from pathlib import Path

import pytest

from amendment_helpers import operator_authorize
from ourob.bootstrap.amend import AmendmentLedger
from ourob.config import Config
from ourob.policies.base import PolicySet, ReviewContext
from ourob.policies.rules import (
    POLICY_CLASSES,
    BudgetPolicy,
    CommandAllowlistPolicy,
    JournalIntegrityPolicy,
    LoopBreakerPolicy,
    PathConfinementPolicy,
    PayloadSizePolicy,
    ProtectedPathPolicy,
    default_policy_set,
)
from ourob.state.model import Invocation

MUTATING = {
    "write_file": True,
    "edit_file": True,
    "delete_file": True,
    "read_file": False,
    "deploy": True,
}


def ctx(
    repo: Path,
    skill: str,
    args: dict | None = None,
    *,
    budget_used: int = 0,
    budget_limit: int = 24,
    history: list | None = None,
    services: dict | None = None,
) -> ReviewContext:
    base: dict = {"mutating_skills": MUTATING}
    base.update(services or {})
    return ReviewContext(
        repo=repo,
        config=Config.load(repo),
        invocation=Invocation(skill=skill, args=args or {}),
        step_index=budget_used,
        budget_used=budget_used,
        budget_limit=budget_limit,
        services=base,
        history=history or [],
    )


# -- path confinement -----------------------------------------------------


def test_confinement_allows_paths_inside_the_repository(repo: Path) -> None:
    verdict = PathConfinementPolicy().review(ctx(repo, "write_file", {"path": "src/ourob/x.py"}))
    assert verdict.allowed


def test_confinement_blocks_traversal(repo: Path) -> None:
    verdict = PathConfinementPolicy().review(ctx(repo, "write_file", {"path": "../../etc/passwd"}))
    assert not verdict.allowed
    assert "outside" in verdict.reason


def test_confinement_blocks_absolute_escapes(repo: Path) -> None:
    assert not PathConfinementPolicy().review(ctx(repo, "write_file", {"path": "/etc/hosts"})).allowed


# -- protected paths ------------------------------------------------------


def test_protected_write_is_refused_without_an_amendment(repo: Path) -> None:
    verdict = ProtectedPathPolicy().review(
        ctx(repo, "write_file", {"path": "src/ourob/policies/base.py", "content": "x"})
    )
    assert not verdict.allowed
    assert "amendment" in verdict.reason


def test_protected_read_is_allowed(repo: Path) -> None:
    assert (
        ProtectedPathPolicy().review(ctx(repo, "read_file", {"path": "src/ourob/policies/base.py"})).allowed
    )


def test_ordinary_write_is_allowed(repo: Path) -> None:
    assert (
        ProtectedPathPolicy()
        .review(ctx(repo, "write_file", {"path": "src/ourob/skills/contrib/new.py", "content": "x"}))
        .allowed
    )


def test_proposal_alone_does_not_authorize_a_protected_write(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/policies/"], "tighten a rule", proposed_by="test")
    verdict = ProtectedPathPolicy().review(
        ctx(
            repo,
            "write_file",
            {"path": "src/ourob/policies/base.py", "content": "x"},
            services={"ledger": ledger},
        )
    )
    assert not verdict.allowed
    assert ledger.authorising_id(amendment.amendment_id) is None


def test_separately_authorized_amendment_covers_only_its_path(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/policies/"], "tighten a rule", proposed_by="test")
    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    verdict = ProtectedPathPolicy().review(
        ctx(
            repo,
            "write_file",
            {"path": "src/ourob/policies/base.py", "content": "x"},
            services={"ledger": ledger},
        )
    )
    assert verdict.allowed, verdict.reason
    assert amendment.amendment_id in verdict.reason
    outside = ProtectedPathPolicy().review(
        ctx(
            repo,
            "write_file",
            {"path": "ourob.toml", "content": "x"},
            services={"ledger": ledger},
        )
    )
    assert not outside.allowed


def test_a_rejected_amendment_authorises_nothing(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/policies/"], "attempt", proposed_by="test")
    ledger.set_status(amendment.amendment_id, "rejected")
    verdict = ProtectedPathPolicy().review(
        ctx(
            repo,
            "write_file",
            {"path": "src/ourob/policies/base.py", "content": "x"},
            services={"ledger": ledger},
        )
    )
    assert not verdict.allowed


def test_config_protected_patterns_cover_directories_and_files(repo: Path) -> None:
    config = Config.load(repo)
    assert config.is_protected("src/ourob/policies/rules.py")
    assert config.is_protected("ourob.toml")
    assert not config.is_protected("src/ourob/kernel.py")
    assert not config.is_protected("tests/test_policies.py")


# -- journal integrity ----------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        ".ourob/journal/run-x.jsonl",
        ".ourob/journal/run-x.jsonl.head",
        ".ourob/index.jsonl",
        ".ourob/system.jsonl",
        ".ourob/verify/run-x.json",
        ".ourob/amendments/amd-x.json",
        ".ourob/snapshots/x/tree/a.py",
    ],
)
def test_history_is_immutable(repo: Path, target: str) -> None:
    verdict = JournalIntegrityPolicy().review(ctx(repo, "write_file", {"path": target, "content": ""}))
    assert not verdict.allowed
    assert "immutable" in verdict.reason


def test_runtime_snapshots_are_immutable(repo: Path) -> None:
    verdict = JournalIntegrityPolicy().review(
        ctx(repo, "write_file", {"path": ".ourob/snapshots/x/tree/a.py", "content": ""})
    )
    assert not verdict.allowed
    assert "immutable" in verdict.reason


# -- budget, payload, commands, loops -------------------------------------


def test_budget_blocks_at_the_limit(repo: Path) -> None:
    assert BudgetPolicy().review(ctx(repo, "read_file", {}, budget_used=24, budget_limit=24)).allowed is False
    assert BudgetPolicy().review(ctx(repo, "read_file", {}, budget_used=23, budget_limit=24)).allowed


def test_payload_size_blocks_oversized_writes(repo: Path) -> None:
    verdict = PayloadSizePolicy().review(
        ctx(repo, "write_file", {"path": "a.py", "content": "x" * 1_100_000})
    )
    assert not verdict.allowed
    assert "limit" in verdict.reason


def test_command_allowlist_blocks_unknown_commands(repo: Path) -> None:
    verdict = CommandAllowlistPolicy().review(ctx(repo, "run_command", {"argv": ["bash", "-c", "ls"]}))
    assert not verdict.allowed


def test_command_allowlist_blocks_deny_patterns(repo: Path) -> None:
    verdict = CommandAllowlistPolicy().review(
        ctx(repo, "run_command", {"argv": ["git", "status", "&&", "rm", "-rf", "/"]})
    )
    assert not verdict.allowed
    assert "deny pattern" in verdict.reason


def test_command_allowlist_permits_listed_commands(repo: Path) -> None:
    assert (
        CommandAllowlistPolicy()
        .review(ctx(repo, "run_command", {"argv": ["python", "-m", "pytest", "-q"]}))
        .allowed
    )


def test_command_allowlist_ignores_other_skills(repo: Path) -> None:
    assert CommandAllowlistPolicy().review(ctx(repo, "read_file", {"path": "a"})).allowed


def test_loop_breaker_stops_a_repeated_failure(repo: Path) -> None:
    invocation = {"skill": "read_file", "args": {"path": "missing.py"}}
    import json

    key = json.dumps(invocation, sort_keys=True)
    history = [{"key": key, "ok": False} for _ in range(3)]
    verdict = LoopBreakerPolicy().review(ctx(repo, "read_file", {"path": "missing.py"}, history=history))
    assert not verdict.allowed


def test_loop_breaker_allows_a_different_call(repo: Path) -> None:
    import json

    key = json.dumps({"skill": "read_file", "args": {"path": "missing.py"}}, sort_keys=True)
    history = [{"key": key, "ok": False} for _ in range(5)]
    verdict = LoopBreakerPolicy().review(ctx(repo, "read_file", {"path": "other.py"}, history=history))
    assert verdict.allowed


def test_a_declared_path_parameter_is_confined(repo: Path) -> None:
    """The contract: a skill says which parameters carry paths, and confinement
    honours that regardless of what the key is called."""
    context = ctx(
        repo,
        "deploy",
        {"destination_file": "../../etc/passwd"},
        services={"skill_params": {"deploy": {"destination_file": {"type": "str", "path": True}}}},
    )
    assert context.declared_path_params() == {"destination_file"}
    verdict = PathConfinementPolicy().review(context)
    assert not verdict.allowed
    assert "destination_file" in verdict.reason


def test_an_undeclared_unusual_key_is_not_confined_by_the_policy(repo: Path) -> None:
    """Without a declaration the policy cannot know -- which is exactly why the
    skill-contract gate refuses to register such a skill at all."""
    context = ctx(repo, "deploy", {"destination_file": "../../etc/passwd"})
    assert context.declared_path_params() == set()
    assert PathConfinementPolicy().review(context).allowed


def test_conventional_key_names_are_still_confined_without_a_declaration(repo: Path) -> None:
    """PATH_KEYS is the net underneath the contract, not a replacement for it."""
    context = ctx(repo, "mystery_skill", {"path": "../../etc/passwd"})
    assert context.declared_path_params() == set()
    assert not PathConfinementPolicy().review(context).allowed


def test_a_declared_path_reaches_the_protected_path_policy(repo: Path) -> None:
    """Declaring a path also brings it under the amendment rule."""
    context = ctx(
        repo,
        "deploy",
        {"destination_file": "src/ourob/policies/rules.py"},
        services={"skill_params": {"deploy": {"destination_file": {"type": "str", "path": True}}}},
    )
    assert not ProtectedPathPolicy().review(context).allowed


def test_the_advisory_hole_warner_is_retired() -> None:
    """Replaced by a declared contract plus a static gate, not a warning."""
    assert "path-key-coverage" not in POLICY_CLASSES
    assert not hasattr(__import__("ourob.policies.rules", fromlist=["x"]), "PathKeyCoveragePolicy")
    assert len(POLICY_CLASSES) == 8
    assert "child-filesystem" in POLICY_CLASSES


# -- the set --------------------------------------------------------------


def test_policy_set_is_deny_wins(repo: Path) -> None:
    policies = PolicySet([PathConfinementPolicy(), ProtectedPathPolicy(), BudgetPolicy()])
    outcome = policies.review(ctx(repo, "write_file", {"path": "../../x", "content": ""}))
    assert not outcome.allowed
    assert "path-confinement" in outcome.reason()


def test_policy_set_allows_a_clean_call(repo: Path) -> None:
    outcome = default_policy_set().review(
        ctx(repo, "write_file", {"path": "src/ourob/skills/contrib/a.py", "content": "x"})
    )
    assert outcome.allowed, outcome.reason()


def test_a_clean_call_produces_no_warnings(repo: Path) -> None:
    outcome = default_policy_set().review(ctx(repo, "write_file", {"path": "src/a.py", "content": "x"}))
    assert outcome.allowed
    assert outcome.warnings == []
    assert all(d.allowed for d in outcome.decisions)


def test_a_crashing_policy_fails_closed(repo: Path) -> None:
    from ourob.policies.base import Policy, Verdict

    class Exploding(Policy):
        name = "exploding"
        title = "Exploding"
        description = "raises on purpose"

        def review(self, ctx: ReviewContext) -> Verdict:  # pragma: no cover - must raise
            raise RuntimeError("boom")

    outcome = PolicySet([Exploding()]).review(ctx(repo, "read_file", {}))
    assert not outcome.allowed
    assert "boom" in outcome.reason()


def test_every_policy_declares_metadata() -> None:
    for name, cls in POLICY_CLASSES.items():
        assert cls.name == name
        assert cls.title
        assert cls.description
        assert isinstance(cls.blocking, bool)


def test_policy_set_catalogue_is_complete() -> None:
    catalogue = default_policy_set().catalogue()
    assert {c["name"] for c in catalogue} == set(POLICY_CLASSES)


def test_raise_if_denied_raises(repo: Path) -> None:
    from ourob.errors import PolicyViolation

    outcome = default_policy_set().review(ctx(repo, "write_file", {"path": "../escape"}))
    assert not outcome.allowed
    with pytest.raises(PolicyViolation) as excinfo:
        outcome.raise_if_denied()
    assert excinfo.value.policy == "path-confinement"
