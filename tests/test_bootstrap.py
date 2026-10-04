"""The bootstrap mechanism: cold start, snapshots, amendments, promotion."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from amendment_helpers import operator_authorize
from ourob.bootstrap.amend import Amendment, AmendmentLedger, AmendmentStatus
from ourob.bootstrap.coldstart import boot, locate_source_root
from ourob.bootstrap.manifest import Manifest, compare
from ourob.bootstrap.promote import Promotion
from ourob.bootstrap.snapshot import Snapshot
from ourob.errors import BootstrapError

FAILING_TEST = "def test_deliberate_failure():\n    assert False, 'on purpose'\n"


# -- cold start -----------------------------------------------------------


def test_boot_verifies_and_imports(repo: Path) -> None:
    module, report = boot(repo)
    assert module is not None
    assert module.__version__
    assert report.trusted
    assert report.drift.clean
    assert report.source_root == repo / "src"


def test_boot_refuses_protected_drift(repo: Path) -> None:
    (repo / "src" / "ourob" / "bootstrap" / "manifest.py").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(BootstrapError) as excinfo:
        boot(repo)
    assert "protected paths have drifted" in str(excinfo.value)


def test_boot_allows_engineered_drift(repo: Path) -> None:
    (repo / "NEW_MODULE.md").write_text("ordinary engineering\n", encoding="utf-8")
    module, report = boot(repo)
    assert module is not None
    assert not report.drift.clean
    assert report.drift.added == ["NEW_MODULE.md"]


def test_boot_can_be_told_to_trust_drift(repo: Path) -> None:
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# amended\n", encoding="utf-8")
    module, report = boot(repo, trust_drift=True)
    assert module is not None
    assert report.forced is True
    assert report.trusted is True
    assert any("protected drift accepted" in n for n in report.notes)


def test_boot_rebuilds_the_lock(repo: Path) -> None:
    before = Manifest.load(repo)
    (repo / "src" / "ourob" / "kernel.py").write_text("# rewritten\n", encoding="utf-8")
    _module, report = boot(repo, rebuild_lock=True)
    after = Manifest.load(repo)
    assert after.version == before.version + 1
    assert report.drift.clean
    assert "rewritten" not in compare(after, repo).describe()


def test_boot_reports_a_missing_lock(repo: Path) -> None:
    (repo / "bootstrap.lock.json").unlink()
    module, report = boot(repo)
    assert module is not None
    assert any("unanchored" in note for note in report.notes)


def test_boot_report_serialises(repo: Path) -> None:
    _module, report = boot(repo)
    payload = report.to_dict()
    assert payload["trusted"] is True
    assert report.describe().startswith("repo")


def test_source_root_is_found(repo: Path) -> None:
    assert locate_source_root(repo) == repo / "src"


def test_source_root_lookup_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError):
        locate_source_root(tmp_path)


def test_the_root_bootstrap_script_runs_standalone(repo: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "bootstrap.py", "--prove"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "verdict      TRUSTED" in proc.stdout


def test_the_root_bootstrap_script_ignores_generated_coverage_data(repo: Path) -> None:
    (repo / ".coverage").write_text("generated", encoding="utf-8")
    (repo / ".coverage.worker").write_text("generated", encoding="utf-8")
    (repo / "coverage").mkdir()
    (repo / "coverage" / "index.html").write_text("generated", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "bootstrap.py", "--prove", "--strict"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "verdict      TRUSTED" in proc.stdout
    assert ".coverage" not in proc.stdout


def test_the_root_bootstrap_script_refuses_protected_drift(repo: Path) -> None:
    # Tamper with a protected file other than the bootstrap itself, then let the
    # intact bootstrap judge the tree.
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# tampered\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "bootstrap.py", "doctor"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1
    assert "refusing to boot" in proc.stderr
    assert "src/ourob/policies/rules.py" in proc.stderr


def test_the_bootstrap_proceeds_when_told_to(repo: Path) -> None:
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# tampered\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "bootstrap.py", "--trust-drift", "--prove"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0
    assert "verdict      NOT TRUSTED" in proc.stdout
    assert "VIOLATIONS" in proc.stdout


def test_the_bootstrap_passes_option_values_through_to_the_cli(repo: Path) -> None:
    """`--message X` must reach argparse intact, not be stripped as a flag."""
    (repo / "docs" / "NOTES.md").write_text("# notes\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "bootstrap.py", "promote", "--message", "a real message", "--no-git"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "promotion ACCEPTED" in proc.stdout
    assert "a real message" in Manifest.load(repo).notes


def test_a_leading_rebuild_reanchors_but_a_subcommand_rebuild_does_not(repo: Path) -> None:
    before = Manifest.load(repo).version

    proc = subprocess.run(
        [sys.executable, "bootstrap.py", "manifest", "--rebuild", "--note", "via the cli"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert Manifest.load(repo).version == before + 1
    assert Manifest.load(repo).notes == "via the cli"


def test_the_bootstrap_script_never_trusts_its_own_removal_from_the_protected_list(
    repo: Path,
) -> None:
    """Even with bootstrap.py stripped from ourob.toml, the script still guards itself."""
    config = repo / "ourob.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace('    "bootstrap.py",\n', ""), encoding="utf-8")
    subprocess.run(
        [sys.executable, "bootstrap.py", "--rebuild", "re-anchored"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sys.path.insert(0, str(repo))
    try:
        import importlib

        import bootstrap as root_bootstrap

        importlib.reload(root_bootstrap)
        protected = root_bootstrap.read_protected()
        assert "bootstrap.py" in protected  # the hard-coded floor
    finally:
        sys.path.remove(str(repo))
        sys.modules.pop("bootstrap", None)


# -- snapshots ------------------------------------------------------------


def test_snapshot_restores_modified_files(repo: Path) -> None:
    original = (repo / "src" / "ourob" / "kernel.py").read_text(encoding="utf-8")
    snapshot = Snapshot.capture(repo, "run-snap")
    (repo / "src" / "ourob" / "kernel.py").write_text("# mangled\n", encoding="utf-8")

    outcome = snapshot.restore()
    assert "src/ourob/kernel.py" in outcome["restored"]
    assert (repo / "src" / "ourob" / "kernel.py").read_text(encoding="utf-8") == original


def test_snapshot_removes_files_the_run_invented(repo: Path) -> None:
    snapshot = Snapshot.capture(repo, "run-snap2")
    (repo / "INVENTED.py").write_text("x = 1\n", encoding="utf-8")
    assert (repo / "INVENTED.py").exists()

    outcome = snapshot.restore()
    assert "INVENTED.py" in outcome["removed"]
    assert not (repo / "INVENTED.py").exists()


def test_snapshot_is_a_noop_on_an_untouched_tree(repo: Path) -> None:
    snapshot = Snapshot.capture(repo, "run-snap3")
    outcome = snapshot.restore()
    assert outcome == {"restored": [], "removed": []}


def test_snapshot_restore_recovers_deleted_files(repo: Path) -> None:
    snapshot = Snapshot.capture(repo, "run-snap4")
    (repo / "pyproject.toml").unlink()
    outcome = snapshot.restore()
    assert "pyproject.toml" in outcome["restored"]
    assert (repo / "pyproject.toml").is_file()


def test_snapshot_reports_drift_since_capture(repo: Path) -> None:
    snapshot = Snapshot.capture(repo, "run-snap5")
    assert snapshot.drift_since().clean
    (repo / "AFTER.md").write_text("later\n", encoding="utf-8")
    assert snapshot.drift_since().added == ["AFTER.md"]


def test_two_snapshots_of_the_same_run_are_refused(repo: Path) -> None:
    Snapshot.capture(repo, "run-snap6")
    with pytest.raises(BootstrapError):
        Snapshot.capture(repo, "run-snap6")


def test_snapshot_listing_and_discard(repo: Path) -> None:
    Snapshot.capture(repo, "run-snap7")
    assert "run-snap7" in Snapshot.list_for(repo)
    Snapshot.load(repo, "run-snap7").discard()
    assert "run-snap7" not in Snapshot.list_for(repo)


def test_loading_an_unknown_snapshot_fails(repo: Path) -> None:
    with pytest.raises(BootstrapError):
        Snapshot.load(repo, "run-never-happened")


# -- amendments -----------------------------------------------------------


def test_amendment_lifecycle(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/policies/"], "tighten a rule")
    assert amendment.status == AmendmentStatus.PROPOSED
    assert amendment.covers("src/ourob/policies/rules.py")
    assert not amendment.covers("src/ourob/kernel.py")
    assert ledger.authorising("src/ourob/policies/rules.py") is None
    assert ledger.authorising_id(amendment.amendment_id) is None

    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    authorized = ledger.authorising("src/ourob/policies/rules.py")
    assert authorized is not None
    assert authorized.amendment_id == amendment.amendment_id
    assert authorized.status == AmendmentStatus.AUTHORIZED

    ratified = ledger.set_status(amendment.amendment_id, AmendmentStatus.RATIFIED, evidence={"digest": "abc"})
    assert ratified.ratified_at
    assert ratified.evidence == {"digest": "abc"}
    assert ledger.authorising("src/ourob/policies/rules.py") is None
    assert ledger.load(amendment.amendment_id).status == AmendmentStatus.RATIFIED


def test_amendment_cannot_name_an_unprotected_path(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    with pytest.raises(BootstrapError, match="only currently protected paths"):
        ledger.propose(["docs/ARCHITECTURE.md"], "attempt to overreach authorization scope")


def test_amendment_authorization_requires_matching_confirmation(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    with pytest.raises(BootstrapError, match="confirmation did not match"):
        ledger.authorize(
            amendment.amendment_id,
            confirmation="different-proposal",
            authorized_by="operator-test",
        )
    assert ledger.authorising_id(amendment.amendment_id) is None


def test_amendment_requires_a_rationale(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    with pytest.raises(BootstrapError):
        ledger.propose(["ourob.toml"], "   ")


def test_amendment_requires_paths(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    with pytest.raises(BootstrapError):
        ledger.propose([], "because")


def test_amendment_ids_cannot_escape(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    with pytest.raises(BootstrapError):
        ledger.load("../escape")


def test_amendment_survives_a_round_trip(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    again = Amendment.from_dict(ledger.load(amendment.amendment_id).to_dict())
    assert again.amendment_id == amendment.amendment_id
    assert again.paths == amendment.paths
    assert again.rationale == amendment.rationale


def test_proposal_file_cannot_claim_authorized_status(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    path = ledger._path(amendment.amendment_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "authorized"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BootstrapError, match="invalid stored amendment status"):
        ledger.load(amendment.amendment_id)
    assert ledger.authorising_id(amendment.amendment_id) is None


@pytest.mark.parametrize(
    ("field", "stale_value", "message"),
    [
        ("base_revision", "stale-revision", "base revision"),
        ("base_digest", "f" * 64, "base lock digest"),
    ],
)
def test_stale_proposal_cannot_be_authorized(repo: Path, field: str, stale_value: str, message: str) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    setattr(amendment, field, stale_value)
    ledger.save(amendment)
    with pytest.raises(BootstrapError, match=message):
        operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    assert ledger.authorising_id(amendment.amendment_id) is None


def test_proposal_changed_after_display_cannot_be_authorized(repo: Path) -> None:
    from ourob.bootstrap.amend import _confirmed_operator_action
    from ourob.fsx import sha256_file

    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    proposal_sha256 = sha256_file(ledger._path(amendment.amendment_id))
    with _confirmed_operator_action(amendment.amendment_id, amendment.amendment_id, proposal_sha256):
        amendment.rationale = "changed after operator display"
        ledger.save(amendment)
        with pytest.raises(BootstrapError, match="exact proposal revision and digest"):
            ledger.authorize(
                amendment.amendment_id,
                confirmation=amendment.amendment_id,
                authorized_by="operator-test",
            )
    assert ledger.authorising_id(amendment.amendment_id) is None


def test_modified_proposal_invalidates_its_prior_grant(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    path = ledger._path(amendment.amendment_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["paths"] = ["src/ourob/policies/"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ledger.authorising_id(amendment.amendment_id) is None


def test_proposal_cannot_retroactively_authorize_existing_drift(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/policies/rules.py"], "change a rule")
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# changed before grant\n", encoding="utf-8")
    with pytest.raises(BootstrapError, match="cannot retroactively authorize"):
        operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("base_revision", "stale-revision"),
        ("base_digest", "f" * 64),
        ("paths", ["src/ourob/policies/"]),
    ],
)
def test_wrong_pin_or_path_in_forged_grant_does_not_authorize(
    repo: Path, field: str, wrong_value: str | list[str]
) -> None:
    from ourob.bootstrap.amend import _confirmed_operator_action
    from ourob.errors import StateError
    from ourob.fsx import sha256_file
    from ourob.state.store import Journal, StateStore

    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    payload = {
        "amendment_id": amendment.amendment_id,
        "authorization_id": "auth-forged",
        "authorized_by": "operator-test",
        "proposal_sha256": sha256_file(ledger._path(amendment.amendment_id)),
        "paths": amendment.paths,
        "base_revision": amendment.base_revision,
        "base_digest": amendment.base_digest,
    }
    payload[field] = wrong_value
    state = StateStore(repo)
    with pytest.raises(StateError, match="interactive operator confirmation"):
        state.record_system("amendment.authorized", payload)
    assert not (state.root / "system.jsonl").exists()

    # Even a correctly hash-chained raw event with a wrong pinned revision or
    # digest is not resolved as authority for the current proposal.
    with _confirmed_operator_action(
        amendment.amendment_id, amendment.amendment_id, payload["proposal_sha256"]
    ):
        Journal(state.root / "system.jsonl").append("amendment.authorized", payload)
    assert ledger.authorising_id(amendment.amendment_id) is None
    # The event is syntactically valid and hash-chained, but the mismatched pin
    # does not authorize the current proposal.
    assert all(item["ok"] for item in state.check_chain())


def test_duplicate_authorization_append_is_rejected_before_history_changes(repo: Path) -> None:
    from ourob.bootstrap.amend import _confirmed_operator_action
    from ourob.errors import StateError
    from ourob.state.store import Journal, StateStore

    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    state = StateStore(repo)
    grant = next(event for event in state.system_events() if event["kind"] == "amendment.authorized")
    with (
        _confirmed_operator_action(
            amendment.amendment_id, amendment.amendment_id, grant["payload"]["proposal_sha256"]
        ),
        pytest.raises(StateError, match="duplicated or replayed"),
    ):
        Journal(state.root / "system.jsonl").append("amendment.authorized", grant["payload"])
    assert len([event for event in state.system_events() if event["kind"] == "amendment.authorized"]) == 1
    assert ledger.authorising_id(amendment.amendment_id) is not None
    assert all(item["ok"] for item in state.check_chain())


def test_terminal_amendment_event_prevents_grant_replay(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "change the budget")
    original = ledger._path(amendment.amendment_id).read_bytes()
    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    ledger.set_status(amendment.amendment_id, AmendmentStatus.REJECTED)
    # Simulate restoring an old proposal snapshot after it was consumed.
    ledger._path(amendment.amendment_id).write_bytes(original)
    assert ledger.authorising_id(amendment.amendment_id) is None


def test_an_amendment_for_one_path_authorises_nothing_else(repo: Path) -> None:
    """The scope of an amendment is exactly the paths it names."""
    amendment = Amendment(
        amendment_id="amd-test",
        paths=["src/ourob/policies/", "ourob.toml"],
        rationale="narrow",
    )
    assert amendment.covers("src/ourob/policies/rules.py")
    assert amendment.covers("src/ourob/policies/base.py")
    assert amendment.covers("src/ourob/policies/nested/deep.py")
    assert amendment.covers("ourob.toml")
    assert amendment.covers("src/ourob/policies")  # the directory entry itself
    assert not amendment.covers("src/ourob/kernel.py")
    assert not amendment.covers("bootstrap.py")
    assert not amendment.covers("ourob.toml.bak")
    assert not amendment.covers("src/ourob/policyx/rules.py")


# -- promotion ------------------------------------------------------------


def test_promote_accepts_a_verified_change(repo: Path) -> None:
    (repo / "docs" / "NOTES.md").write_text("# notes\n\nengineering drift.\n", encoding="utf-8")
    before = Manifest.load(repo)

    result = Promotion(repo, use_git=False).promote(
        message="add engineering notes", gates=["compile", "import", "manifest", "policy-integrity"]
    )
    assert result.accepted, result.describe()
    after = Manifest.load(repo)
    assert after.version == before.version + 1
    assert "docs/NOTES.md" in after.paths()
    assert compare(after, repo).clean


def test_promote_refuses_protected_drift_without_an_amendment(repo: Path) -> None:
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# tampered\n", encoding="utf-8")
    result = Promotion(repo, use_git=False).promote(gates=["compile"])
    assert not result.accepted
    assert result.protected == ["src/ourob/policies/rules.py"]
    assert any("no amendment" in m for m in result.messages)


def test_promote_rolls_back_when_verification_fails(repo: Path) -> None:
    (repo / "tests" / "test_broken_change.py").write_text(FAILING_TEST, encoding="utf-8")
    snapshot = Snapshot.capture(repo, "run-fail")

    result = Promotion(repo, use_git=False, rollback_on_failure=True, snapshot_run_id="run-fail").promote(
        message="a change that breaks the build", gates=["compile", "tests"]
    )
    assert not result.accepted
    assert any("verification failed" in m for m in result.messages)
    assert "tests/test_broken_change.py" in result.rollback["removed"]
    assert not (repo / "tests" / "test_broken_change.py").exists()
    # the lock was never rewritten
    assert compare(Manifest.load(repo), repo).clean
    assert snapshot.drift_since().clean


def test_promote_can_be_told_to_keep_a_failing_tree(repo: Path) -> None:
    (repo / "tests" / "test_broken_change.py").write_text(FAILING_TEST, encoding="utf-8")
    result = Promotion(repo, use_git=False, rollback_on_failure=False).promote(gates=["compile", "tests"])
    assert not result.accepted
    assert (repo / "tests" / "test_broken_change.py").exists()


def test_promote_with_an_amendment_ratifies_it(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/policies/"], "lower the retry threshold")
    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("MAX_REPEATS = 3", "MAX_REPEATS = 2"),
        encoding="utf-8",
    )

    result = Promotion(repo, use_git=False).promote(
        amendment_id=amendment.amendment_id,
        message="lower MAX_REPEATS to 2",
        gates=["compile", "import", "manifest", "policy-integrity", "skill-contract"],
    )
    assert result.accepted, result.describe()
    assert ledger.load(amendment.amendment_id).status == AmendmentStatus.RATIFIED
    assert compare(Manifest.load(repo), repo).clean


def test_promote_rejects_an_amendment_that_does_not_cover_the_change(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["ourob.toml"], "only the config")
    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# tampered\n", encoding="utf-8")

    result = Promotion(repo, use_git=False).promote(amendment_id=amendment.amendment_id, gates=["compile"])
    assert not result.accepted
    assert any("does not authorise" in m for m in result.messages)


def test_promote_rejects_an_unknown_amendment(repo: Path) -> None:
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# tampered\n", encoding="utf-8")
    result = Promotion(repo, use_git=False).promote(amendment_id="amd-nope", gates=["compile"])
    assert not result.accepted
    assert any("could not be loaded" in m for m in result.messages)


def test_a_failed_promotion_marks_the_amendment_rejected(repo: Path) -> None:
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/verify/"], "break a gate on purpose")
    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    (repo / "src" / "ourob" / "verify" / "gates.py").write_text("this is not python(\n", encoding="utf-8")

    result = Promotion(repo, use_git=False, rollback_on_failure=False).promote(
        amendment_id=amendment.amendment_id, gates=["compile"]
    )
    assert not result.accepted
    assert ledger.load(amendment.amendment_id).status == AmendmentStatus.REJECTED
    assert ledger.load(amendment.amendment_id).evidence


def test_promotion_result_describes_itself(repo: Path) -> None:
    (repo / "docs" / "NOTES.md").write_text("# notes\n", encoding="utf-8")
    result = Promotion(repo, use_git=False).promote(message="notes", gates=["compile"])
    text = result.describe()
    assert "promotion ACCEPTED" in text
    assert "compile" in text
    assert result.to_dict()["accepted"] is True


def test_promote_commits_when_git_is_available(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "baseline"],
        cwd=repo,
        check=True,
    )
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    amendment = ledger.propose(["src/ourob/policies/"], "tighten the retry threshold")
    operator_authorize(ledger, amendment.amendment_id, authorized_by="operator-test")
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("MAX_REPEATS = 3", "MAX_REPEATS = 2"),
        encoding="utf-8",
    )
    result = Promotion(repo).promote(
        amendment_id=amendment.amendment_id,
        message="tighten retry threshold under git",
        gates=["compile", "policy-integrity"],
    )
    assert result.accepted, result.describe()
    assert result.git_sha
    assert ledger.load(amendment.amendment_id).status == AmendmentStatus.RATIFIED
    assert ledger.authorising_id(amendment.amendment_id) is None
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout.strip() == ""
