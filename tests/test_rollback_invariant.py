"""The rollback invariant.

Restoring a snapshot must return the repository **exactly** to the snapshot
state, *including* ``bootstrap.lock.json``, and must never treat the lock as an
ordinary generated artifact.

This file exists because that invariant was broken once. ``Snapshot.restore``
deleted every file not named in the manifest, and the lock is deliberately *not*
in the manifest -- so rolling back un-anchored the very tree it was restoring.
Rollback is a constitutional recovery mechanism, not a convenience command, so
the property is pinned here rather than left to be rediscovered.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ourob import fsx
from ourob.bootstrap.coldstart import boot
from ourob.bootstrap.manifest import LOCK_NAME, Manifest
from ourob.bootstrap.snapshot import SNAPSHOT_DIR, Snapshot


def fingerprint(repo: Path) -> dict[str, str]:
    """Every tracked file plus the lock, as {path: sha256}."""
    out = {fsx.rel(p, repo): fsx.sha256_file(p) for p in fsx.walk_repo(repo)}
    lock = repo / LOCK_NAME
    if lock.is_file():
        out[LOCK_NAME] = fsx.sha256_file(lock)
    return out


# -- the invariant --------------------------------------------------------


def test_the_lock_is_tracked_separately_from_the_manifest(repo: Path) -> None:
    """Precondition for the bug: the lock is not an ordinary manifest entry."""
    assert LOCK_NAME not in Manifest.load(repo).paths()
    assert (repo / LOCK_NAME).is_file()


def test_restore_reproduces_the_tree_byte_for_byte(repo: Path) -> None:
    before = fingerprint(repo)
    snapshot = Snapshot.capture(repo, "run-inv")

    kernel = (repo / "src" / "ourob" / "kernel.py").read_text(encoding="utf-8")
    (repo / "src" / "ourob" / "kernel.py").write_text("# mangled\n", encoding="utf-8")
    (repo / "pyproject.toml").unlink()
    (repo / "INVENTED.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "docs" / "deep" / "nested" / "ALSO.py").parent.mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "deep" / "nested" / "ALSO.py").write_text("y = 2\n", encoding="utf-8")

    assert fingerprint(repo) != before
    outcome = snapshot.restore()

    assert set(outcome["restored"]) == {"src/ourob/kernel.py", "pyproject.toml"}
    assert set(outcome["removed"]) == {"INVENTED.py", "docs/deep/nested/ALSO.py"}
    assert (repo / "src" / "ourob" / "kernel.py").read_text(encoding="utf-8") == kernel
    assert fingerprint(repo) == before


def test_restore_reproduces_the_bootstrap_lock(repo: Path) -> None:
    original_bytes = (repo / LOCK_NAME).read_bytes()
    original = Manifest.load(repo)
    snapshot = Snapshot.capture(repo, "run-lock")

    # Re-anchoring rewrites the lock: new version, new digest, new timestamp.
    subprocess.run(
        [sys.executable, "bootstrap.py", "--rebuild", "re-anchored mid-run"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    reanchored = Manifest.load(repo)
    assert reanchored.version == original.version + 1
    assert (repo / LOCK_NAME).read_bytes() != original_bytes

    outcome = snapshot.restore()

    assert LOCK_NAME in outcome["restored"]
    assert (repo / LOCK_NAME).read_bytes() == original_bytes
    restored = Manifest.load(repo)
    assert restored.version == original.version
    assert restored.digest == original.digest
    assert restored.notes == original.notes


def test_restore_recovers_a_deleted_lock(repo: Path) -> None:
    original_bytes = (repo / LOCK_NAME).read_bytes()
    snapshot = Snapshot.capture(repo, "run-lostlock")

    (repo / LOCK_NAME).unlink()
    assert not (repo / LOCK_NAME).is_file()

    outcome = snapshot.restore()
    assert LOCK_NAME in outcome["restored"]
    assert (repo / LOCK_NAME).read_bytes() == original_bytes


def test_restore_never_deletes_the_lock(repo: Path) -> None:
    """The lock is absent from the manifest, so a naive 'delete the unknown' pass
    would remove it.  It must survive even when nothing else changed."""
    snapshot = Snapshot.capture(repo, "run-nodelete")
    outcome = snapshot.restore()
    assert LOCK_NAME not in outcome["removed"]
    assert (repo / LOCK_NAME).is_file()


def test_restore_survives_a_lock_that_was_missing_at_capture(repo: Path) -> None:
    """An unanchored tree still rolls back; it just has no lock to restore."""
    (repo / LOCK_NAME).unlink()
    subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'src');"
         "from ourob.bootstrap.manifest import Manifest; Manifest.build('.').save()"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    snapshot = Snapshot.capture(repo, "run-unanchored")
    (repo / LOCK_NAME).unlink()
    (repo / "NEW.py").write_text("x = 1\n", encoding="utf-8")

    outcome = snapshot.restore()
    assert LOCK_NAME in outcome["restored"]  # rebuilt at capture, so it comes back
    assert outcome["removed"] == ["NEW.py"]


def test_restore_is_idempotent(repo: Path) -> None:
    snapshot = Snapshot.capture(repo, "run-idem")
    (repo / "src" / "ourob" / "kernel.py").write_text("# mangled\n", encoding="utf-8")
    first = snapshot.restore()
    second = snapshot.restore()
    assert first["restored"] == ["src/ourob/kernel.py"]
    assert second == {"restored": [], "removed": []}


def test_the_tree_is_trusted_again_after_a_rollback(repo: Path) -> None:
    """End to end: a rolled-back tree cold-starts as a ratified version again."""
    Snapshot.capture(repo, "run-recover")
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# tampered\n", encoding="utf-8")
    (repo / "bootstrap.py").write_text("# tampered\n", encoding="utf-8")

    from ourob.errors import BootstrapError

    with pytest.raises(BootstrapError):
        boot(repo)

    Snapshot.load(repo, "run-recover").restore()

    _module, report = boot(repo)
    assert report.trusted
    assert report.drift.clean


def test_rollback_leaves_no_drift_against_the_snapshot(repo: Path) -> None:
    snapshot = Snapshot.capture(repo, "run-drift")
    for name in ("a.py", "b/c.py"):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    assert not snapshot.drift_since().clean
    snapshot.restore()
    assert snapshot.drift_since().clean


def test_snapshot_state_itself_is_never_a_rollback_candidate(repo: Path) -> None:
    """Snapshots live under .ourob/ and must not delete each other."""
    first = Snapshot.capture(repo, "run-a")
    second = Snapshot.capture(repo, "run-b")
    outcome = first.restore()
    assert not any(path.startswith(SNAPSHOT_DIR) for path in outcome["removed"])
    assert Snapshot.load(repo, "run-b").root.is_dir()
    assert second.files == first.files


def test_snapshot_metadata_records_the_manifest_it_came_from(repo: Path) -> None:
    manifest = Manifest.load(repo)
    snapshot = Snapshot.capture(repo, "run-meta")
    meta = json.loads((snapshot.root / "snapshot.json").read_text(encoding="utf-8"))
    assert meta["manifest_digest"] == manifest.digest
    assert {f["path"] for f in meta["files"]} == set(manifest.paths())
    assert all(f["sha256"] for f in meta["files"])
