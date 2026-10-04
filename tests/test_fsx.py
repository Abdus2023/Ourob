"""Path confinement is the single most load-bearing guarantee in the runtime."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ourob import fsx
from ourob.errors import PathEscapeError


def test_confine_accepts_relative_paths_inside_the_root(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert fsx.confine(tmp_path, "a/b.py") == (tmp_path / "a" / "b.py").resolve()


def test_confine_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        fsx.confine(tmp_path, "../escape.py")


def test_confine_rejects_disguised_traversal(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        fsx.confine(tmp_path, "a/../../escape.py")


def test_confine_rejects_absolute_paths_outside_the_root(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        fsx.confine(tmp_path, "/etc/passwd")


def test_confine_accepts_absolute_paths_inside_the_root(tmp_path: Path) -> None:
    inside = (tmp_path / "inside.py").resolve()
    assert fsx.confine(tmp_path, str(inside)) == inside


def test_confine_rejects_symlinks_pointing_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(target)
    except OSError:  # pragma: no cover - platforms without symlink support
        pytest.skip("symlinks are not available here")
    with pytest.raises(PathEscapeError):
        fsx.confine(root, "link")


def test_atomic_write_is_visible_and_complete(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "file.txt"
    fsx.atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert not list(tmp_path.glob("**/.ourob-tmp-*"))


def test_atomic_write_overwrites_in_place(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    fsx.atomic_write(target, "one")
    fsx.atomic_write(target, "two")
    assert target.read_text(encoding="utf-8") == "two"


def test_atomic_write_honours_the_requested_mode(tmp_path: Path) -> None:
    target = tmp_path / "private.txt"
    fsx.atomic_write(target, "private", mode=0o600)
    assert target.stat().st_mode & 0o777 == 0o600


def test_walk_repo_skips_generated_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("", encoding="utf-8")
    (tmp_path / ".coverage").write_text("generated", encoding="utf-8")
    (tmp_path / ".coverage.worker").write_text("generated", encoding="utf-8")
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage" / "index.html").write_text("generated", encoding="utf-8")
    names = {fsx.rel(p, tmp_path) for p in fsx.walk_repo(tmp_path)}
    assert names == {"src/real.py"}


def test_walk_repo_is_deterministic(tmp_path: Path) -> None:
    for name in ("b.py", "a.py", "c.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    first = [fsx.rel(p, tmp_path) for p in fsx.walk_repo(tmp_path)]
    second = [fsx.rel(p, tmp_path) for p in fsx.walk_repo(tmp_path)]
    assert first == second == ["a.py", "b.py", "c.py"]


def test_repo_root_walks_up_to_the_marker(tmp_path: Path) -> None:
    (tmp_path / fsx.REPO_MARKER).write_text("", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert fsx.repo_root(deep) == tmp_path.resolve()


def test_repo_root_refuses_to_guess(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        fsx.repo_root(tmp_path)


def test_sha256_helpers_agree(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_bytes(b"payload")
    assert fsx.sha256_file(target) == fsx.sha256_bytes(b"payload")
    assert fsx.sha256_text("payload") == fsx.sha256_bytes(b"payload")


def test_confine_never_returns_a_path_outside_root_for_random_inputs(tmp_path: Path) -> None:
    hostiles = ["..", "../..", "/", "./../x", "a/../../../etc/shadow", "~/.ssh/id_rsa"]
    for hostile in hostiles:
        try:
            result = fsx.confine(tmp_path, hostile)
        except PathEscapeError:
            continue
        assert str(result).startswith(os.path.realpath(tmp_path))
