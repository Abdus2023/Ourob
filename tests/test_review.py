"""Human review: what a run actually changed, and whether promotion says so.

The gates can tell you a change is *sound*. They cannot show it to a person.
These tests pin the claim that what ``ourob diff`` displays is the same set of
files the journal recorded, and that a promotion names them.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from amendment_helpers import operator_authorize
from ourob.bootstrap.amend import AmendmentLedger
from ourob.bootstrap.snapshot import Snapshot
from ourob.cli import main as cli_main
from ourob.kernel import Kernel
from ourob.planner.scripted import Plan, ScriptedPlanner
from ourob.state.model import RunStatus
from ourob.state.store import StateStore


def run_cli(repo: Path, *argv: str) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = cli_main(["--repo", str(repo), *argv])
    return code, buffer.getvalue()


def execute(repo: Path, plan: Plan):
    kernel = Kernel(repo, verify_at_end=False)
    return kernel.run(plan.goal, ScriptedPlanner(plan), max_steps=plan.max_steps)


WRITE_PLAN = Plan.from_dict(
    {
        "goal": "add two new files",
        "max_steps": 4,
        "steps": [
            {
                "skill": "write_file",
                "args": {"path": "notes/alpha.md", "content": "# alpha\nfirst line\n"},
            },
            {
                "skill": "write_file",
                "args": {"path": "notes/beta.md", "content": "# beta\n"},
            },
            {"skill": "finish", "args": {"summary": "wrote two files", "success": True}},
        ],
    }
)

EDIT_PLAN = Plan.from_dict(
    {
        "goal": "change an existing file",
        "max_steps": 4,
        "steps": [
            {
                "skill": "edit_file",
                "args": {
                    "path": "README.md",
                    "old_text": "# ourob",
                    "new_text": "# ourob (reviewed)",
                },
            },
            {"skill": "finish", "args": {"summary": "edited the readme", "success": True}},
        ],
    }
)

READ_ONLY_PLAN = Plan.from_dict(
    {
        "goal": "look only",
        "max_steps": 3,
        "steps": [
            {"skill": "list_dir", "args": {"path": "src"}},
            {"skill": "finish", "args": {"summary": "looked", "success": True}},
        ],
    }
)


# -- the run record -------------------------------------------------------


def test_a_run_records_what_it_touched(repo: Path) -> None:
    outcome = execute(repo, WRITE_PLAN)
    assert outcome.run.status is RunStatus.COMPLETED
    assert outcome.touched == ["notes/alpha.md", "notes/beta.md"]


def test_touched_is_reconstructed_from_the_journal(repo: Path) -> None:
    outcome = execute(repo, WRITE_PLAN)
    replayed = StateStore(repo).replay(outcome.run.run_id)
    assert replayed.touched == outcome.touched


def test_touched_deduplicates_in_first_touch_order(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "touch the same file twice",
            "max_steps": 4,
            "steps": [
                {"skill": "write_file", "args": {"path": "a.txt", "content": "1"}},
                {"skill": "write_file", "args": {"path": "a.txt", "content": "2"}},
                {"skill": "write_file", "args": {"path": "b.txt", "content": "3"}},
                {"skill": "finish", "args": {"summary": "done", "success": True}},
            ],
        }
    )
    outcome = execute(repo, plan)
    assert outcome.touched == ["a.txt", "b.txt"]


def test_a_read_only_run_touches_nothing(repo: Path) -> None:
    outcome = execute(repo, READ_ONLY_PLAN)
    assert outcome.touched == []
    assert outcome.snapshot == ""


def test_denied_steps_do_not_count_as_touched(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "try and fail",
            "max_steps": 3,
            "steps": [
                {"skill": "write_file", "args": {"path": "../escape.txt", "content": "x"}},
                {"skill": "finish", "args": {"summary": "denied", "success": True}},
            ],
        }
    )
    outcome = execute(repo, plan)
    assert outcome.denied == 1
    assert outcome.touched == []


# -- ourob diff -----------------------------------------------------------


def test_diff_lists_exactly_what_the_journal_recorded(repo: Path) -> None:
    outcome = execute(repo, WRITE_PLAN)
    code, out = run_cli(repo, "diff", outcome.run.run_id, "--stat")
    assert code == 0, out

    listed = [line.split()[-1] for line in out.splitlines() if line.startswith("  ") and line.strip()]
    assert listed == outcome.touched
    assert "2 file(s) touched" in out
    assert "added" in out


def test_diff_renders_a_real_unified_diff_for_an_edit(repo: Path) -> None:
    outcome = execute(repo, EDIT_PLAN)
    code, out = run_cli(repo, "diff", outcome.run.run_id)
    assert code == 0, out

    assert "--- a/README.md" in out
    assert "+++ b/README.md" in out
    assert "@@" in out
    assert "-# ourob" in out
    assert "+# ourob (reviewed)" in out


def test_diff_shows_the_full_content_of_a_new_file(repo: Path) -> None:
    outcome = execute(repo, WRITE_PLAN)
    code, out = run_cli(repo, "diff", outcome.run.run_id)
    assert code == 0, out
    assert "added: notes/alpha.md" in out
    assert "+# alpha" in out
    assert "+first line" in out


def test_diff_of_a_read_only_run_says_so(repo: Path) -> None:
    outcome = execute(repo, READ_ONLY_PLAN)
    code, out = run_cli(repo, "diff", outcome.run.run_id)
    assert code == 0
    assert "recorded no file changes" in out


def test_diff_reports_a_pre_existing_file_that_was_deleted(repo: Path) -> None:
    """README.md existed in the snapshot, so its absence now is a deletion."""
    outcome = execute(repo, EDIT_PLAN)
    (repo / "README.md").unlink()
    code, out = run_cli(repo, "diff", outcome.run.run_id, "--stat")
    assert code == 0
    assert "deleted" in out


def test_diff_reports_a_file_added_and_then_removed_as_vanished(repo: Path) -> None:
    """Absent before and absent now: the change cancelled itself out."""
    outcome = execute(repo, WRITE_PLAN)
    (repo / "notes" / "alpha.md").unlink()
    code, out = run_cli(repo, "diff", outcome.run.run_id, "--stat")
    assert code == 0
    assert "vanished" in out


def test_diff_after_a_rollback_reports_the_file_as_unchanged(repo: Path) -> None:
    """Ties review to recovery: a rolled-back change reads as no change."""
    outcome = execute(repo, EDIT_PLAN)
    assert (repo / "README.md").read_text(encoding="utf-8").startswith("# ourob (reviewed)")

    Snapshot.load(repo, outcome.snapshot).restore()

    code, out = run_cli(repo, "diff", outcome.run.run_id)
    assert code == 0
    assert "unchanged: README.md" in out
    assert "restored to its pre-run content" in out


def test_diff_without_a_snapshot_degrades_honestly(repo: Path) -> None:
    outcome = execute(repo, WRITE_PLAN)
    Snapshot.load(repo, outcome.snapshot).discard()
    code, out = run_cli(repo, "diff", outcome.run.run_id)
    assert code == 0
    assert "no pre-run baseline" in out


def test_diff_of_an_unknown_run_is_an_error(repo: Path) -> None:
    code, out = run_cli(repo, "diff", "run-never-happened")
    assert code == 1
    assert "StateError" in out


def test_diff_caps_long_output(repo: Path) -> None:
    plan = Plan.from_dict(
        {
            "goal": "write a long file",
            "max_steps": 3,
            "steps": [
                {
                    "skill": "write_file",
                    "args": {"path": "long.txt", "content": "".join(f"line {i}\n" for i in range(80))},
                },
                {"skill": "finish", "args": {"summary": "done", "success": True}},
            ],
        }
    )
    outcome = execute(repo, plan)
    code, out = run_cli(repo, "diff", outcome.run.run_id, "--max-lines", "10")
    assert code == 0
    assert "more diff line(s)" in out


# -- promotion ------------------------------------------------------------


def test_promotion_names_the_files_it_is_accepting(repo: Path) -> None:
    execute(repo, WRITE_PLAN)
    code, out = run_cli(
        repo, "promote", "--message", "add notes", "--no-git", "--gates", "compile", "manifest"
    )
    assert code == 0, out
    assert "promotion ACCEPTED" in out
    assert "- notes/alpha.md" in out
    assert "- notes/beta.md" in out


def test_promotion_names_protected_files_separately(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/policies/"], "tighten a rule")
    operator_authorize(ledger, amendment.amendment_id, authorized_by="review-test")
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("MAX_REPEATS = 3", "MAX_REPEATS = 2"),
        encoding="utf-8",
    )

    code, out = run_cli(
        repo,
        "promote",
        "--amendment",
        amendment.amendment_id,
        "--message",
        "tighten",
        "--no-git",
        "--no-rollback",
        "--gates",
        "compile",
        "manifest",
    )
    assert code == 0, out
    assert "protected paths touched: src/ourob/policies/rules.py" in out
    assert "- src/ourob/policies/rules.py" in out
