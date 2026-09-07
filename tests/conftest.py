"""Test fixtures.

``repo`` is a full copy of this repository in a temporary directory with a fresh
bootstrap lock.  Tests that modify the runtime operate on the copy, so the real
tree is never touched -- and the copy's own ``tests/`` directory is replaced with
a single trivial file, so the ``tests`` gate inside the copy cannot recurse back
into this suite.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REAL_REPO = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns(
    ".git", ".ourob", "__pycache__", ".pytest_cache", "*.egg-info", "bootstrap.lock.json"
)

SMOKE_TEST = '''\
def test_the_runtime_still_boots():
    import ourob

    assert ourob.__version__
'''


@pytest.fixture(scope="session")
def real_repo() -> Path:
    return REAL_REPO


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A private, fully working copy of the runtime."""
    target = tmp_path / "ourob"
    shutil.copytree(REAL_REPO, target, ignore=IGNORE)
    tests = target / "tests"
    if tests.is_dir():
        shutil.rmtree(tests)
    tests.mkdir()
    (tests / "test_smoke.py").write_text(SMOKE_TEST, encoding="utf-8")
    subprocess.run(
        [sys.executable, "bootstrap.py", "--rebuild", "test fixture baseline"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return target


@pytest.fixture
def store(repo: Path):
    from ourob.state.store import StateStore

    return StateStore(repo)


@pytest.fixture
def config(repo: Path):
    from ourob.config import Config

    return Config.load(repo)


@pytest.fixture
def registry(repo: Path):
    from ourob.skills.registry import SkillRegistry

    reg = SkillRegistry(repo)
    reg.discover()
    return reg


@pytest.fixture
def skill_ctx(repo: Path, store, config):
    from ourob.skills.base import SkillContext
    from ourob.skills.registry import SkillRegistry

    reg = SkillRegistry(repo)
    reg.discover()
    return SkillContext(
        repo=repo,
        run_id="test-run",
        store=store,
        services={"config": config, "registry": reg, "amended_paths": []},
    )
