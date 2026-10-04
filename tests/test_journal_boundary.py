"""The runtime's own state must be immutable to normal and child capabilities."""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

from ourob.bootstrap.amend import AmendmentLedger
from ourob.kernel import Kernel
from ourob.planner.scripted import Plan, ScriptedPlanner
from ourob.state.store import StateStore


def _protected_artifacts(repo: Path) -> tuple[StateStore, str, Path, list[Path]]:
    store = StateStore(repo)
    victim = store.open_run("journal mutation regression", planner="test")
    store.close_run(victim)
    store.record_system("promotion.rejected", {"accepted": False, "amendment_id": ""})
    proposal = AmendmentLedger(repo / ".ourob" / "amendments").propose(
        ["ourob.toml"], "test proposal for state immutability", proposed_by="test"
    )
    artifacts = [
        store.journals / f"{victim.run_id}.jsonl",
        store.journals / f"{victim.run_id}.jsonl.head",
        store.root / "index.jsonl",
        store.root / "index.jsonl.head",
        store.root / "system.jsonl",
        store.root / "system.jsonl.head",
        AmendmentLedger(repo / ".ourob" / "amendments")._path(proposal.amendment_id),
    ]
    assert all(path.is_file() for path in artifacts)
    assert all(item["ok"] for item in store.check_chain())
    return store, victim.run_id, artifacts[-1], artifacts


def _file_hashes(paths: list[Path]) -> dict[str, str]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_direct_file_skills_cannot_rewrite_runtime_history(repo: Path) -> None:
    store, victim_id, proposal_path, paths = _protected_artifacts(repo)
    before = _file_hashes([path for path in paths if path.name not in {"index.jsonl", "index.jsonl.head"}])
    proposal_rel = proposal_path.relative_to(repo).as_posix()
    victim_journal = f".ourob/journal/{victim_id}.jsonl"
    victim_head = victim_journal + ".head"
    system = ".ourob/system.jsonl"
    system_head = system + ".head"
    plan = Plan.from_dict(
        {
            "goal": "attempt direct journal and promotion-history rewrites",
            "max_steps": 9,
            "steps": [
                {"skill": "write_file", "args": {"path": victim_journal, "content": "truncated"}},
                {
                    "skill": "edit_file",
                    "args": {"path": victim_head, "old_text": "hash", "new_text": "forged"},
                },
                {"skill": "delete_file", "args": {"path": ".ourob/index.jsonl"}},
                {
                    "skill": "write_file",
                    "args": {"path": system, "content": '{\\"kind\\":\\"promotion.accepted\\"}'},
                },
                {
                    "skill": "edit_file",
                    "args": {"path": system_head, "old_text": "hash", "new_text": "forged"},
                },
                {"skill": "delete_file", "args": {"path": system}},
                {"skill": "delete_file", "args": {"path": system_head}},
                {"skill": "delete_file", "args": {"path": proposal_rel}},
                {"skill": "finish", "args": {"summary": "all state writes were denied", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, state=store, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.denied == 8
    immutable_paths = [path for path in paths if path.name not in {"index.jsonl", "index.jsonl.head"}]
    assert _file_hashes(immutable_paths) == before
    assert (store.root / "index.jsonl").is_file()

    events = list(store.journal(outcome.run.run_id).events())
    denied = [
        event for event in events if event["kind"] == "policy.decision" and not event["payload"]["allowed"]
    ]
    assert len(denied) == 8
    assert all(
        any(
            decision["policy"] == "journal-integrity" and not decision["allowed"]
            for decision in event["payload"]["decisions"]
        )
        for event in denied
    )
    assert all(item["ok"] for item in store.check_chain())


def test_python_child_cannot_truncate_replace_rename_or_delete_journals(repo: Path) -> None:
    store, victim_id, proposal_path, paths = _protected_artifacts(repo)
    immutable_paths = [path for path in paths if path.name not in {"index.jsonl", "index.jsonl.head"}]
    before = _file_hashes(immutable_paths)
    docs = repo / "docs"
    alias = docs / "journal-alias.jsonl"
    alias.symlink_to(paths[0])
    (docs / "replacement.tmp").write_text("forged replacement\n", encoding="utf-8")

    variants = [
        paths[0].relative_to(repo).as_posix(),
        str(paths[0]),
        f".ourob/journal/../journal/{victim_id}.jsonl",
        alias.relative_to(repo).as_posix(),
        paths[1].relative_to(repo).as_posix(),
        paths[2].relative_to(repo).as_posix(),
        paths[3].relative_to(repo).as_posix(),
        paths[4].relative_to(repo).as_posix(),
        paths[5].relative_to(repo).as_posix(),
        proposal_path.relative_to(repo).as_posix(),
    ]
    script = textwrap.dedent(
        f"""
        import json
        import os
        from pathlib import Path
        from ourob.bootstrap.amend import AmendmentLedger, _confirmed_operator_action
        from ourob.errors import StateError
        from ourob.fsx import sha256_file
        from ourob.state.store import StateStore

        targets = {variants!r}
        outcomes = {{}}
        def attempt(name, action):
            try:
                action()
            except OSError as exc:
                outcomes[name] = f"denied:{{exc.errno}}"
            except Exception as exc:
                outcomes[name] = f"rejected:{{type(exc).__name__}}"
            else:
                outcomes[name] = "allowed"

        for index, target in enumerate(targets):
            attempt(f"write-{{index}}", lambda target=target: Path(target).write_text("forged"))
            attempt(f"truncate-{{index}}", lambda target=target: open(target, "r+b").truncate(0))
            resolved = str(Path(target).resolve())
            attempt(
                f"replace-{{index}}",
                lambda resolved=resolved: os.replace("docs/replacement.tmp", resolved),
            )
            attempt(
                f"rename-{{index}}",
                lambda resolved=resolved, index=index: os.rename(resolved, f"docs/moved-{{index}}"),
            )
            attempt(f"delete-{{index}}", lambda resolved=resolved: os.unlink(resolved))

        amendment_ledger = AmendmentLedger(Path.cwd() / ".ourob" / "amendments")
        proposal_id = {proposal_path.stem!r}
        proposal_sha256 = sha256_file(amendment_ledger._path(proposal_id))
        def request_operator_grant():
            with _confirmed_operator_action(proposal_id, proposal_id, proposal_sha256):
                return amendment_ledger.authorize(
                    proposal_id,
                    confirmation=proposal_id,
                    authorized_by="child-process",
                )
        attempt("runtime-generated-authorization", request_operator_grant)
        attempt(
            "forged-semantic-append",
            lambda: StateStore(Path.cwd()).record_system(
                "promotion.rejected", {{"accepted": True, "amendment_id": ""}}
            ),
        )
        print(json.dumps(outcomes, sort_keys=True))
        """
    )
    plan = Plan.from_dict(
        {
            "goal": "attempt journal mutation from a Python child",
            "max_steps": 3,
            "steps": [
                {"skill": "run_python", "args": {"code": script}},
                {"skill": "finish", "args": {"summary": "child operations were denied", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, state=store, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.denied == 0
    child_step = outcome.run.steps[0]
    assert child_step.result is not None and child_step.result.ok, child_step.result
    outcomes = json.loads(child_step.result.output.splitlines()[-1])
    assert outcomes
    assert all(value.startswith("denied:") or value.startswith("rejected:") for value in outcomes.values()), (
        outcomes
    )
    assert _file_hashes(immutable_paths) == before
    assert (store.root / "index.jsonl").is_file()
    assert all(item["ok"] for item in store.check_chain())

    events = list(store.journal(outcome.run.run_id).events())
    decision = next(event for event in events if event["kind"] == "policy.decision")
    assert decision["payload"]["allowed"] is True
    assert any(d["policy"] == "child-filesystem" and d["allowed"] for d in decision["payload"]["decisions"])


def test_direct_journal_policy_catches_symlinked_runtime_state_root(repo: Path) -> None:
    from ourob.config import Config
    from ourob.policies.base import ReviewContext
    from ourob.policies.rules import JournalIntegrityPolicy
    from ourob.state.model import Invocation

    config = Config.load(repo)
    docs = repo / "docs"
    (repo / ".ourob").symlink_to(docs, target_is_directory=True)
    for path in (".ourob/system.jsonl", "docs/ordinary.txt"):
        context = ReviewContext(
            repo=repo,
            config=config,
            invocation=Invocation(skill="write_file", args={"path": path, "content": "tampered"}),
            services={"mutating_skills": {"write_file": True}},
        )
        verdict = JournalIntegrityPolicy().review(context)
        assert not verdict.allowed, (path, verdict)
        assert verdict.policy == "journal-integrity"
    assert not (docs / "system.jsonl").exists()
    assert not (docs / "ordinary.txt").exists()


def test_semantic_state_api_refuses_forged_promotion_state(repo: Path) -> None:
    store = StateStore(repo)
    before = _file_hashes(
        [store.root / "system.jsonl.head"] if (store.root / "system.jsonl.head").exists() else []
    )
    try:
        store.record_system("promotion.rejected", {"accepted": True, "amendment_id": ""})
    except Exception as exc:
        assert "invalid system event" in str(exc)
    else:
        raise AssertionError("semantically forged promotion state was appended")
    after_paths = [store.root / "system.jsonl", store.root / "system.jsonl.head"]
    assert all(not path.exists() for path in after_paths)
    assert _file_hashes([Path(path) for path in before]) == before
    assert all(item["ok"] for item in store.check_chain())
