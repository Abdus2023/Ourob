"""The self-describing manifest is how the runtime knows what it consists of."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ourob.bootstrap.manifest import LOCK_NAME, Manifest, compare
from ourob.config import Config
from ourob.errors import BootstrapError


def test_lock_exists_and_parses(repo: Path) -> None:
    manifest = Manifest.load(repo)
    assert len(manifest) > 20
    assert manifest.digest
    assert manifest.version >= 1


def test_the_lock_covers_the_runtime(repo: Path) -> None:
    paths = set(Manifest.load(repo).paths())
    for expected in (
        "bootstrap.py",
        "ourob.toml",
        "pyproject.toml",
        "src/ourob/__init__.py",
        "src/ourob/kernel.py",
        "src/ourob/policies/rules.py",
        "src/ourob/verify/gates.py",
        "src/ourob/bootstrap/coldstart.py",
        "src/ourob/skills/registry.py",
        "README.md",
    ):
        assert expected in paths, expected


def test_the_lock_excludes_runtime_state(repo: Path) -> None:
    (repo / ".ourob" / "journal").mkdir(parents=True, exist_ok=True)
    (repo / ".ourob" / "journal" / "run-x.jsonl").write_text("{}", encoding="utf-8")
    (repo / "__pycache__").mkdir(exist_ok=True)
    (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    assert ".ourob/journal/run-x.jsonl" not in Manifest.load(repo).paths()
    assert all("__pycache__" not in p for p in Manifest.load(repo).paths())


def test_protected_flags_match_the_config(repo: Path) -> None:
    manifest = Manifest.load(repo)
    config = Config.load(repo)
    for path in manifest.paths():
        assert manifest.files[path].protected is config.is_protected(path), path


def test_digest_is_stable_and_content_addressed(repo: Path) -> None:
    manifest = Manifest.load(repo)
    assert manifest.digest == Manifest.load(repo).digest
    (repo / "src" / "ourob" / "kernel.py").write_text("# changed\n", encoding="utf-8")
    assert Manifest.build(repo).digest != manifest.digest


def test_compare_reports_added_changed_and_removed(repo: Path) -> None:
    manifest = Manifest.load(repo)
    assert compare(manifest, repo).clean

    (repo / "BRAND_NEW.md").write_text("new\n", encoding="utf-8")
    (repo / "README.md").write_text("rewritten\n", encoding="utf-8")
    (repo / "pyproject.toml").unlink()

    diff = compare(manifest, repo)
    assert diff.added == ["BRAND_NEW.md"]
    assert diff.changed == ["README.md"]
    assert diff.missing == ["pyproject.toml"]
    assert not diff.clean
    assert "added=1" in diff.describe()


def test_clean_tree_reports_no_drift(repo: Path) -> None:
    assert compare(Manifest.load(repo), repo).describe() == "no drift"


def test_touching_a_file_without_changing_it_is_not_drift(repo: Path) -> None:
    target = repo / "README.md"
    target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    assert compare(Manifest.load(repo), repo).clean


def test_manifest_round_trips_through_disk(repo: Path, tmp_path: Path) -> None:
    manifest = Manifest.load(repo)
    payload = json.loads((repo / LOCK_NAME).read_text(encoding="utf-8"))
    assert payload["format"] == "ourob.bootstrap.lock/v1"
    assert payload["digest"] == manifest.digest
    assert len(payload["files"]) == len(manifest)


def test_rebuilding_bumps_the_version(repo: Path) -> None:
    before = Manifest.load(repo)
    after = Manifest.build(repo, notes="test rebuild", version=before.version + 1)
    after.save()
    reloaded = Manifest.load(repo)
    assert reloaded.version == before.version + 1
    assert reloaded.notes == "test rebuild"


def test_loading_without_a_lock_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError):
        Manifest.load(tmp_path)


def test_protected_drift_is_distinguishable(repo: Path) -> None:
    manifest = Manifest.load(repo)
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# tampered\n", encoding="utf-8")
    (repo / "src" / "ourob" / "kernel.py").write_text("# engineered\n", encoding="utf-8")
    diff = compare(manifest, repo)
    assert sorted(diff.changed) == [
        "src/ourob/kernel.py",
        "src/ourob/policies/rules.py",
    ]
    assert diff.protected_changes(manifest) == ["src/ourob/policies/rules.py"]


def test_new_files_inside_protected_directories_are_protected(repo: Path) -> None:
    config = Config.load(repo)
    assert config.is_protected("src/ourob/policies/brand_new_rule.py")
    assert config.is_protected("src/ourob/bootstrap/brand_new.py")
