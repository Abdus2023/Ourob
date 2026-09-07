from __future__ import annotations

import pytest

from ourob.errors import SchemaError
from ourob.schema import check_spec, coerce, validate

SPEC = {
    "path": {"type": "str", "required": True},
    "count": {"type": "int", "default": 3, "min": 1, "max": 10},
    "mode": {"type": "str", "enum": ["fast", "safe"], "default": "safe"},
    "flag": {"type": "bool", "default": False},
}


def test_check_spec_accepts_a_well_formed_table() -> None:
    assert check_spec(SPEC) == []


def test_check_spec_rejects_unknown_types_and_keys() -> None:
    problems = check_spec({"a": {"type": "widget"}, "b": {"wat": 1}})
    assert any("unknown type" in p for p in problems)
    assert any("unknown rule keys" in p for p in problems)


def test_check_spec_rejects_non_mapping_rules() -> None:
    assert check_spec({"a": "str"}) != []


def test_validate_flags_missing_required() -> None:
    assert any("missing required" in p for p in validate({}, SPEC))


def test_validate_flags_wrong_types() -> None:
    problems = validate({"path": 3}, SPEC)
    assert any("must be str" in p for p in problems)


def test_validate_rejects_bool_where_int_is_expected() -> None:
    assert any("got bool" in p for p in validate({"path": "x", "count": True}, SPEC))


def test_validate_enforces_bounds_and_enums() -> None:
    assert any(">= 1" in p for p in validate({"path": "x", "count": 0}, SPEC))
    assert any("<= 10" in p for p in validate({"path": "x", "count": 99}, SPEC))
    assert any("must be one of" in p for p in validate({"path": "x", "mode": "slow"}, SPEC))


def test_validate_rejects_unexpected_parameters() -> None:
    assert any("unexpected parameter" in p for p in validate({"path": "x", "wat": 1}, SPEC))


def test_coerce_fills_defaults() -> None:
    assert coerce({"path": "a"}, SPEC) == {
        "path": "a",
        "count": 3,
        "mode": "safe",
        "flag": False,
    }


def test_coerce_raises_on_invalid_calls() -> None:
    with pytest.raises(SchemaError):
        coerce({}, SPEC)


def test_none_is_treated_as_absent() -> None:
    assert coerce({"path": "a", "count": None}, SPEC)["count"] == 3
