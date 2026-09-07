"""The CLI is a thin wrapper, but it is the surface an operator actually touches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ourob.cli import main


def run(repo: Path, *argv: str) -> tuple[int, str]:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = main(["--repo", str(repo), *argv])
    return code, buffer.getvalue()


def test_version(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "ourob" in capsys.readouterr().out


def test_no_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_doctor_reports_every_subsystem(repo: Path) -> None:
    code, out = run(repo, "doctor")
    assert code == 0, out
    for needle in (
        "repository",
        "skills",
        "policies",
        "gates",
        "protected",
        "lock",
        "drift        no drift",
        "protected    CLEAN",
        "journal",
    ):
        assert needle in out, needle


def test_doctor_flags_protected_drift(repo: Path) -> None:
    (repo / "src" / "ourob" / "verify" / "gates.py").write_text("# tampered\n", encoding="utf-8")
    code, out = run(repo, "doctor")
    assert code == 0  # doctor reports, it does not judge
    assert "DRIFT: src/ourob/verify/gates.py" in out


def test_doctor_reports_a_missing_lock(repo: Path) -> None:
    (repo / "bootstrap.lock.json").unlink()
    code, out = run(repo, "doctor")
    assert code == 0
    assert "MISSING" in out


def test_skills_lists_the_catalogue(repo: Path) -> None:
    code, out = run(repo, "skills")
    assert code == 0
    assert "write_file" in out and "mutating: True" in out
    assert "read_file" in out and "mutating: False" in out


def test_skills_json_is_machine_readable(repo: Path) -> None:
    code, out = run(repo, "skills", "--json")
    assert code == 0
    data = json.loads(out)
    assert {entry["name"] for entry in data} >= {"read_file", "write_file", "finish"}
    for entry in data:
        assert entry["description"]
        assert isinstance(entry["params"], dict)


def test_policies_and_gates_are_listed(repo: Path) -> None:
    code, out = run(repo, "policies")
    assert code == 0 and "protected-paths" in out

    code, out = run(repo, "policies", "--json")
    assert json.loads(out)

    code, out = run(repo, "gates")
    assert code == 0
    assert "policy-integrity" in out and "blocking" in out


def test_verify_passes_on_a_clean_tree(repo: Path) -> None:
    code, out = run(repo, "verify")
    assert code == 0, out
    assert "verification PASSED" in out


def test_verify_fails_on_a_broken_tree(repo: Path) -> None:
    (repo / "src" / "ourob" / "kernel.py").write_text("def oops(:\n", encoding="utf-8")
    code, out = run(repo, "verify", "--gates", "compile", "import")
    assert code == 1
    assert "verification FAILED" in out


def test_verify_ignores_unknown_gates_but_says_so(repo: Path) -> None:
    code, out = run(repo, "verify", "--gates", "compile", "not-a-gate")
    assert code == 0
    assert "unknown gate(s) ignored: not-a-gate" in out


def test_manifest_inspection(repo: Path) -> None:
    code, out = run(repo, "manifest")
    assert code == 0
    assert "lock      " in out and "drift     no drift" in out

    code, out = run(repo, "manifest", "--list")
    assert "P src/ourob/policies/rules.py" in out
    assert "  src/ourob/kernel.py" in out


def test_manifest_shows_drift(repo: Path) -> None:
    (repo / "NEW.md").write_text("x\n", encoding="utf-8")
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# t\n", encoding="utf-8")
    code, out = run(repo, "manifest")
    assert code == 0
    assert "added    NEW.md" in out
    assert "changed  src/ourob/policies/rules.py [protected]" in out


def test_manifest_rebuild(repo: Path) -> None:
    (repo / "NEW.md").write_text("x\n", encoding="utf-8")
    code, out = run(repo, "manifest", "--rebuild", "--note", "re-anchored")
    assert code == 0
    assert "lock rewritten" in out
    assert "version 2" in out  # a rebuild bumps the version rather than resetting it and "version" in out
    code, out = run(repo, "manifest")
    assert "drift     no drift" in out
    assert "re-anchored" in out


def test_manifest_without_a_lock(repo: Path) -> None:
    (repo / "bootstrap.lock.json").unlink()
    code, out = run(repo, "manifest")
    assert code == 1
    assert "no bootstrap.lock.json" in out


def test_run_executes_a_plan(repo: Path) -> None:
    code, out = run(repo, "run", "plans/healthcheck.json", "-v")
    assert code == 0, out
    assert "goal: Inspect the runtime without changing anything" in out
    assert "completed" in out
    assert "verification PASSED" in out


def test_run_reports_a_failed_plan(repo: Path) -> None:
    plan = repo / "plans" / "bad.json"
    plan.write_text(
        json.dumps(
            {
                "goal": "fail on purpose",
                "max_steps": 3,
                "steps": [
                    {"skill": "write_file", "args": {"path": "src/ourob/x.py", "content": "def (:"}},
                    {"skill": "finish", "args": {"summary": "done", "success": True}},
                ],
            }
        ),
        encoding="utf-8",
    )
    code, out = run(repo, "run", "plans/bad.json")
    assert code == 1
    assert "verification FAILED" in out
    assert "pre-run snapshot" in out


def test_run_emits_json(repo: Path) -> None:
    code, out = run(repo, "run", "plans/healthcheck.json", "--json")
    assert code == 0
    start = out.index("{")
    data = json.loads(out[start:])
    assert data["goal"].startswith("Inspect the runtime")
    assert len(data["steps"]) == 5


def test_runs_and_journal(repo: Path) -> None:
    run(repo, "run", "plans/healthcheck.json")
    code, out = run(repo, "runs")
    assert code == 0 and "Inspect the runtime" in out

    run_id = out.strip().splitlines()[0].split()[0]
    code, out = run(repo, "journal", run_id)
    assert code == 0
    assert "run.started" in out and "skill.result" in out

    code, out = run(repo, "journal", "--check")
    assert code == 0
    assert all("ok  " in line for line in out.strip().splitlines())


def test_show_replays_a_run(repo: Path) -> None:
    run(repo, "run", "plans/refused.json")
    code, out = run(repo, "runs")
    run_id = out.strip().splitlines()[0].split()[0]
    code, out = run(repo, "show", run_id)
    assert code == 0
    assert "policy path-confinement" in out
    assert "[denied]" in out


def test_journal_check_detects_tampering(repo: Path) -> None:
    run(repo, "run", "plans/healthcheck.json")
    journals = sorted((repo / ".ourob" / "journal").glob("*.jsonl"))
    assert journals
    lines = journals[0].read_text(encoding="utf-8").splitlines(keepends=True)
    payload = json.loads(lines[1])
    payload["payload"] = {"rewritten": True}
    lines[1] = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    journals[0].write_text("".join(lines), encoding="utf-8")

    code, out = run(repo, "journal", "--check")
    assert code == 1
    assert "BAD " in out


def test_amend_and_list(repo: Path) -> None:
    code, out = run(repo, "amend", "src/ourob/policies/", "--why", "tighten a rule")
    assert code == 0
    amendment_id = out.split("proposed ")[1].split()[0]
    assert "ourob promote --amendment" in out

    code, out = run(repo, "amend", "--list")
    assert code == 0 and amendment_id in out and "[proposed]" in out

    code, out = run(repo, "ratify", amendment_id, "--message", "tightened", "--no-git")
    assert code == 0
    assert "ratified" in out

    code, out = run(repo, "amend", "--list")
    assert "[ratified]" in out


def test_amend_requires_paths(repo: Path) -> None:
    code, out = run(repo, "amend")
    assert code == 2
    assert "usage:" in out


def test_promote_flow(repo: Path) -> None:
    (repo / "docs" / "NOTES.md").write_text("# notes\n", encoding="utf-8")
    code, out = run(repo, "promote", "--message", "add notes", "--no-git")
    assert code == 0, out
    assert "promotion ACCEPTED" in out
    assert "lock rewritten" in out


def test_promote_refuses_protected_drift(repo: Path) -> None:
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# t\n", encoding="utf-8")
    code, out = run(repo, "promote", "--no-git")
    assert code == 1
    assert "promotion REJECTED" in out
    assert "no amendment" in out


def test_coldstart(repo: Path) -> None:
    code, out = run(repo, "coldstart")
    assert code == 0
    assert "trusted     True" in out


def test_coldstart_refuses_protected_drift(repo: Path) -> None:
    (repo / "bootstrap.py").write_text("# tampered\n", encoding="utf-8")
    code, out = run(repo, "coldstart")
    assert code == 1
    assert "BOOT FAILED" in out


def test_snapshots_listing(repo: Path) -> None:
    code, out = run(repo, "snapshots")
    assert code == 0 and "no snapshots" in out

    run(repo, "run", "plans/self_extend.json")
    code, out = run(repo, "snapshots")
    assert code == 0 and "drift since" in out

    run_id = out.strip().splitlines()[0].split()[0]
    code, out = run(repo, "snapshots", "--discard", run_id)
    assert code == 0 and "discarded snapshot" in out


def test_snapshot_rollback_restores_the_tree(repo: Path) -> None:
    run(repo, "run", "plans/self_extend.json")
    code, out = run(repo, "snapshots")
    run_id = out.strip().splitlines()[0].split()[0]

    assert (repo / "tests" / "test_contrib_rot13.py").is_file()
    code, out = run(repo, "snapshots", "--rollback", run_id)
    assert code == 0
    assert "rolled back to snapshot" in out
    assert not (repo / "tests" / "test_contrib_rot13.py").exists()
    assert "drift since snapshot: no drift" in out


def test_selftest(repo: Path) -> None:
    code, out = run(repo, "selftest")
    assert code == 0, out
    assert "self-test PASSED" in out


def test_errors_are_reported_not_raised(repo: Path) -> None:
    code, out = run(repo, "journal", "run-does-not-exist")
    # an empty journal is not an error, it is simply empty
    assert code == 0
    code, out = run(repo, "show", "run-does-not-exist")
    assert code == 1
    assert "StateError" in out
