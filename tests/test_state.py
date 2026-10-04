"""The journal is the runtime's memory; it must be append-only and tamper-evident."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ourob.errors import StateError
from ourob.state.model import (
    Invocation,
    Run,
    RunStatus,
    SkillResult,
    StepStatus,
    VerificationReport,
)
from ourob.state.store import Journal, StateStore


def test_state_store_rejects_a_symlinked_runtime_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    sentinel = docs / "keep.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")
    (repo / ".ourob").symlink_to(docs, target_is_directory=True)

    with pytest.raises(StateError, match="must not be a symlink"):
        StateStore(repo)
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"
    assert list(docs.iterdir()) == [sentinel]


def test_journal_round_trips_events(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j.jsonl")
    journal.append("a", {"n": 1})
    journal.append("b", {"n": 2})
    events = list(Journal(tmp_path / "j.jsonl").events())
    assert [e["kind"] for e in events] == ["a", "b"]
    assert [e["seq"] for e in events] == [1, 2]


def test_journal_chain_verifies(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j.jsonl")
    for i in range(5):
        journal.append("tick", {"i": i})
    ok, detail = Journal(tmp_path / "j.jsonl").check_chain()
    assert ok, detail
    assert "5 events" in detail


def test_appending_preserves_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    Journal(path).append("one", {})
    Journal(path).append("two", {})
    ok, _ = Journal(path).check_chain()
    assert ok


@pytest.mark.parametrize("tamper", ["edit", "delete", "truncate"])
def test_tampering_is_detected(tmp_path: Path, tamper: str) -> None:
    path = tmp_path / "j.jsonl"
    journal = Journal(path)
    for i in range(4):
        journal.append("tick", {"i": i})

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if tamper == "edit":
        payload = json.loads(lines[1])
        payload["payload"]["i"] = 999
        lines[1] = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    elif tamper == "delete":
        del lines[1]
    else:
        lines = lines[:2]
    path.write_text("".join(lines), encoding="utf-8")

    ok, detail = Journal(path).check_chain()
    assert not ok
    assert any(
        marker in detail
        for marker in ("mismatch", "does not match", "truncated", "sequence is not contiguous")
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "delete",
        "wrong-sequence",
        "wrong-hash",
        "malformed",
        "string-sequence",
        "boolean-sequence",
        "extra-field",
        "duplicate-key",
    ],
)
def test_head_anchor_tampering_is_detected_and_blocks_append(tmp_path: Path, tamper: str) -> None:
    path = tmp_path / "head.jsonl"
    journal = Journal(path)
    journal.append("tick", {"i": 1})
    head = path.with_name(path.name + ".head")
    if tamper == "delete":
        head.unlink()
    elif tamper == "malformed":
        head.write_text("not json", encoding="utf-8")
    elif tamper == "duplicate-key":
        data = json.loads(head.read_text(encoding="utf-8"))
        head.write_text(
            f'{{"seq":{data["seq"]},"seq":{data["seq"]},"hash":"{data["hash"]}"}}',
            encoding="utf-8",
        )
    else:
        data = json.loads(head.read_text(encoding="utf-8"))
        if tamper == "wrong-sequence":
            data["seq"] = 0
        elif tamper == "string-sequence":
            data["seq"] = str(data["seq"])
        elif tamper == "boolean-sequence":
            data["seq"] = True
        elif tamper == "wrong-hash":
            data["hash"] = "f" * 64
        elif tamper == "extra-field":
            data["comment"] = "not part of the canonical anchor"
        head.write_text(json.dumps(data), encoding="utf-8")

    intact, detail = Journal(path).check_chain()
    assert not intact
    assert any(word in detail for word in ("anchor", "truncated", "unreadable"))
    with pytest.raises(StateError, match="refusing to append to corrupt journal"):
        Journal(path).append("later", {})


def test_store_opens_and_closes_a_run(repo: Path, store: StateStore) -> None:
    run = store.open_run("do a thing")
    store.record(run.run_id, "step.planned", {"index": 0, "invocation": {"skill": "x", "args": {}}})
    run.status = RunStatus.COMPLETED
    run.outcome = "done"
    store.close_run(run)

    listing = store.list_runs()
    assert len(listing) == 1
    assert listing[0]["status"] == RunStatus.COMPLETED.value


def test_replay_reconstructs_a_run(repo: Path, store: StateStore) -> None:
    run = store.open_run("replay me", planner="scripted")
    invocation = Invocation(skill="write_file", args={"path": "a.txt", "content": "hi"})
    store.record(run.run_id, "step.planned", {"index": 0, "invocation": invocation.to_dict()})
    store.record(
        run.run_id,
        "policy.decision",
        {"index": 0, "allowed": True, "decisions": [{"policy": "p", "allowed": True, "reason": "ok"}]},
    )
    result = SkillResult(ok=True, output="wrote a.txt", data={"path": "a.txt"})
    store.record(run.run_id, "skill.result", {"index": 0, "result": result.to_dict()})
    run.status = RunStatus.COMPLETED
    run.outcome = "finished"
    store.close_run(run)

    replayed = store.replay(run.run_id)
    assert replayed.goal == "replay me"
    assert replayed.status is RunStatus.COMPLETED
    assert len(replayed.steps) == 1
    step = replayed.steps[0]
    assert step.invocation.skill == "write_file"
    assert step.status is StepStatus.OK
    assert step.result is not None and step.result.output == "wrote a.txt"
    assert step.decisions[0].policy == "p"


def test_denied_steps_survive_replay(repo: Path, store: StateStore) -> None:
    run = store.open_run("denied")
    invocation = Invocation(skill="write_file", args={"path": "../outside"})
    store.record(run.run_id, "step.planned", {"index": 0, "invocation": invocation.to_dict()})
    store.record(
        run.run_id,
        "policy.decision",
        {
            "index": 0,
            "allowed": False,
            "decisions": [
                {"policy": "path-confinement", "allowed": False, "reason": "escape", "severity": "block"}
            ],
        },
    )
    store.record(
        run.run_id,
        "skill.result",
        {"index": 0, "denied": True, "result": SkillResult(ok=False, error="denied").to_dict()},
    )
    store.close_run(run)

    replayed = store.replay(run.run_id)
    assert replayed.steps[0].status is StepStatus.DENIED
    assert len(replayed.denied_steps) == 1


def test_replay_of_an_unknown_run_fails_loudly(store: StateStore) -> None:
    with pytest.raises(StateError):
        store.replay("run-does-not-exist")


def test_verification_reports_round_trip(store: StateStore) -> None:
    report = VerificationReport(run_id="run-x")
    from ourob.state.model import GateResult

    report.gates.append(GateResult(gate="compile", passed=True, summary="ok"))
    report.gates.append(GateResult(gate="tests", passed=False, summary="boom", details="traceback"))
    store.save_report(report)

    loaded = store.latest_report("run-x")
    assert loaded is not None
    assert not loaded.passed
    assert [g.gate for g in loaded.failures] == ["tests"]
    assert store.reports()[0]["digest"] == report.digest[:12]


def test_run_id_cannot_escape_the_journal_directory(store: StateStore) -> None:
    with pytest.raises(StateError):
        store.journal("../escape")


def test_system_journal_is_separate_from_runs(repo: Path, store: StateStore) -> None:
    store.record_system("promotion.rejected", {"accepted": False, "amendment_id": ""})
    kinds = [e["kind"] for e in store.system_events()]
    assert kinds == ["promotion.rejected"]


def test_semantically_forged_system_event_is_refused_and_detected(repo: Path) -> None:
    store = StateStore(repo)
    payload = {"accepted": True, "amendment_id": ""}
    with pytest.raises(StateError, match="refusing to append invalid system event"):
        store.record_system("promotion.rejected", payload)
    assert not (store.root / "system.jsonl").exists()

    # The low-level journal API must reject the same impossible transition
    # before either the event or its head anchor is written.
    with pytest.raises(StateError, match="inconsistent acceptance state"):
        Journal(store.root / "system.jsonl").append("promotion.rejected", payload)
    assert not (store.root / "system.jsonl").exists()
    assert not (store.root / "system.jsonl.head").exists()
    assert all(item["ok"] for item in store.check_chain())


def test_authorization_events_require_confirmed_operator_scope(repo: Path) -> None:
    store = StateStore(repo)
    payload = {"amendment_id": "amd-forged"}
    with pytest.raises(StateError, match="interactive operator confirmation"):
        store.record_system("amendment.authorized", payload)
    with pytest.raises(StateError, match="interactive operator confirmation"):
        Journal(store.root / "system.jsonl").append("amendment.authorized", payload)
    assert not (store.root / "system.jsonl").exists()
    assert not (store.root / "system.jsonl.head").exists()
    assert all(item["ok"] for item in store.check_chain())


def test_runtime_actor_cannot_claim_operator_authorization_in_system_history(repo: Path) -> None:
    from ourob.bootstrap.amend import _confirmed_operator_action

    store = StateStore(repo)
    payload = {
        "amendment_id": "amd-forged",
        "authorization_id": "auth-forged",
        "authorized_by": "runtime",
        "proposal_sha256": "a" * 64,
        "paths": ["ourob.toml"],
        "base_revision": "unversioned",
        "base_digest": "b" * 64,
    }
    with pytest.raises(StateError, match="interactive operator confirmation"):
        store.record_system("amendment.authorized", payload)
    with (
        _confirmed_operator_action(
            payload["amendment_id"], payload["amendment_id"], payload["proposal_sha256"]
        ),
        pytest.raises(StateError, match="not an operator identity"),
    ):
        Journal(store.root / "system.jsonl").append("amendment.authorized", payload)
    assert not (store.root / "system.jsonl").exists()
    assert not (store.root / "system.jsonl.head").exists()
    assert all(item["ok"] for item in store.check_chain())


def test_all_journals_report_chain_status(repo: Path, store: StateStore) -> None:
    run = store.open_run("chain check")
    store.record(run.run_id, "noop", {})
    store.close_run(run)
    results = store.check_chain()
    assert results and all(r["ok"] for r in results)


def test_run_serialisation_is_json_safe() -> None:
    run = Run.new("serialise me")
    step = run.add_step(Invocation(skill="finish", args={"summary": "s"}))
    step.result = SkillResult(ok=True, output="bye")
    payload = run.to_dict()
    assert json.loads(json.dumps(payload))["goal"] == "serialise me"
