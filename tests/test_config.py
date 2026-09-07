"""One source of truth for what is protected, and the floors beneath it.

``ourob.toml`` is authoritative. ``config.DEFAULT_PROTECTED``,
``bootstrap.IMMUTABLE`` and ``PolicyIntegrityGate.REQUIRED_COVERAGE`` are
*floors*: three places that must agree with the shipped config, deliberately
written out separately so that no single edit can lower all of them at once.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ourob.config import DEFAULT_PROTECTED, Config
from ourob.verify.gates import PolicyIntegrityGate

REPO = Path(__file__).resolve().parents[1]


def test_config_cannot_narrow_protection_below_the_floor(tmp_path: Path) -> None:
    """ourob.toml may add protection; it may never take any away.

    Without this the whole "policies are protected" property rests on one
    verify-time gate: bootstrap.py unions only IMMUTABLE, so a narrowed config
    would let a run rewrite its own guardrails and still report TRUSTED.
    """
    (tmp_path / "ourob.toml").write_text('[policy]\nprotected = ["ourob.toml"]\n', encoding="utf-8")
    config = Config.load(tmp_path)
    for path in ("src/ourob/policies/rules.py", "bootstrap.py", "src/ourob/verify/gates.py"):
        assert config.is_protected(path), f"floor must still protect {path}"


def test_config_can_add_protection_beyond_the_floor(tmp_path: Path) -> None:
    (tmp_path / "ourob.toml").write_text(
        '[policy]\nprotected = ["ourob.toml", "src/ourob/kernel.py"]\n', encoding="utf-8"
    )
    config = Config.load(tmp_path)
    assert config.is_protected("src/ourob/kernel.py")
    assert config.is_protected("bootstrap.py")


def test_an_empty_protected_list_still_protects_the_floor(tmp_path: Path) -> None:
    (tmp_path / "ourob.toml").write_text("[policy]\nprotected = []\n", encoding="utf-8")
    assert Config.load(tmp_path).is_protected("bootstrap.py")


def test_the_shipped_config_covers_every_floor() -> None:
    """The four definitions must agree on the shipped repository."""
    protected = {p.rstrip("/") for p in Config.load(REPO).policy.protected}
    for floor_name, floor in (
        ("config.DEFAULT_PROTECTED", DEFAULT_PROTECTED),
        ("PolicyIntegrityGate.REQUIRED_COVERAGE", PolicyIntegrityGate.REQUIRED_COVERAGE),
    ):
        for entry in floor:
            assert entry.rstrip("/") in protected, f"{floor_name} requires {entry}"


def test_bootstrap_immutable_is_a_subset_of_the_floor() -> None:
    """bootstrap.py cannot import the package, so this test is the link."""
    sys.path.insert(0, str(REPO))
    try:
        import bootstrap  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    floor = {p.rstrip("/") for p in DEFAULT_PROTECTED}
    for entry in bootstrap.IMMUTABLE:
        assert entry.rstrip("/") in floor, f"bootstrap.IMMUTABLE lists {entry}, the floor does not"
    assert set(bootstrap.read_protected()) >= {p.rstrip("/") for p in bootstrap.IMMUTABLE}


def test_bootstrap_reads_the_same_list_as_the_config() -> None:
    sys.path.insert(0, str(REPO))
    try:
        import bootstrap  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    from_config = {p.rstrip("/") for p in Config.load(REPO).policy.protected}
    from_bootstrap = {p.rstrip("/") for p in bootstrap.read_protected()}
    assert from_bootstrap == from_config, (
        f"bootstrap.py and Config disagree: {from_bootstrap ^ from_config}"
    )

