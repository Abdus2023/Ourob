"""Tests for the self-authored rot13 skill."""

from pathlib import Path

from ourob.skills.base import SkillContext
from ourob.skills.registry import SkillRegistry
from ourob.state.model import Invocation

REPO = Path(__file__).resolve().parents[1]


def _registry() -> SkillRegistry:
    registry = SkillRegistry(REPO)
    registry.discover()
    return registry


def _rot13(text: str) -> str:
    ctx = SkillContext(repo=REPO, services={})
    result = _registry().dispatch(Invocation(skill="rot13", args={"text": text}), ctx)
    assert result.ok, result.error
    return result.output


def test_rot13_is_discovered_without_an_install_step() -> None:
    assert _registry().has("rot13")


def test_rot13_rotates() -> None:
    assert _rot13("Hello") == "Uryyb"


def test_rot13_is_its_own_inverse() -> None:
    assert _rot13(_rot13("ourob")) == "ourob"


def test_rot13_leaves_non_letters_alone() -> None:
    assert _rot13("123 !") == "123 !"
