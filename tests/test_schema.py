from __future__ import annotations

import pytest

from ourob.errors import SchemaError
from ourob.schema import check_spec, coerce, validate

SPEC = {
    "path": {"type": "str", "required": True, "path": True},
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


def test_check_spec_requires_declared_paths_to_be_strings() -> None:
    assert check_spec({"n": {"type": "int", "path": True}}) != []
    assert check_spec({"p": {"type": "str", "path": True}}) == []


def test_check_spec_rejects_a_non_boolean_path_flag() -> None:
    problems = check_spec({"p": {"type": "str", "path": "yes"}})
    assert any("must be a boolean" in p for p in problems)


@pytest.mark.parametrize(
    "name",
    ["path", "file", "dir", "target", "destination", "target_file", "out_path", "cache_dir"],
)
def test_a_path_like_string_parameter_must_declare_itself(name: str) -> None:
    problems = check_spec({name: {"type": "str"}})
    assert any("does not declare path: true" in p for p in problems), problems
    assert check_spec({name: {"type": "str", "path": True}}) == []


@pytest.mark.parametrize("name", ["content", "pattern", "rationale", "summary", "code", "text"])
def test_ordinary_string_parameters_are_left_alone(name: str) -> None:
    assert check_spec({name: {"type": "str"}}) == []


def test_non_string_parameters_are_never_required_to_declare_a_path() -> None:
    assert check_spec({"paths": {"type": "list"}, "max_bytes": {"type": "int"}}) == []


def test_looks_like_a_path() -> None:
    from ourob.schema import looks_like_a_path

    assert looks_like_a_path("path")
    assert looks_like_a_path("log_file")
    assert looks_like_a_path("scratch_dir")
    assert not looks_like_a_path("content")
    assert not looks_like_a_path("paths")  # a list of paths is not itself a path


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

