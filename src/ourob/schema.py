"""A deliberately tiny parameter schema, shared by skills and the planner.

Full JSON Schema is overkill here and would need a dependency.  Skills declare
parameters with ``type``/``required``/``default``/``enum``/``min``/``max``/``desc``
and the same table drives three different consumers:

* runtime validation before a skill executes,
* the ``SkillContractGate`` in the verification suite,
* the catalogue handed to a planner so it can emit well-formed tool calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import SchemaError

_TYPE_TABLE: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
    "any": (object,),
}

SPEC_KEYS = frozenset({"type", "required", "default", "enum", "min", "max", "desc", "path"})

#: Parameter names that are path-like by convention.  ``check_spec`` uses this to
#: insist that such a parameter *declares* ``path: true`` rather than relying on
#: the policy layer guessing from the key name.
PATHY_NAMES = frozenset({"path", "file", "dir", "directory", "target", "destination"})
PATHY_SUFFIXES = ("_path", "_file", "_files", "_dir", "_directory", "_destination", "_dest")


def looks_like_a_path(name: str) -> bool:
    """True when a parameter name suggests it carries a filesystem path."""
    return name in PATHY_NAMES or name.endswith(PATHY_SUFFIXES)


def check_spec(spec: Mapping[str, Any], *, owner: str = "schema") -> list[str]:
    """Validate a parameter table itself.  Returns human-readable problems."""
    problems: list[str] = []
    if not isinstance(spec, Mapping):
        return [f"{owner}: parameter table must be a mapping"]
    for name, rule in spec.items():
        if not isinstance(name, str) or not name:
            problems.append(f"{owner}: empty parameter name")
            continue
        if not isinstance(rule, Mapping):
            problems.append(f"{owner}.{name}: rule must be a mapping")
            continue
        unknown = set(rule) - SPEC_KEYS
        if unknown:
            problems.append(f"{owner}.{name}: unknown rule keys {sorted(unknown)}")
        kind = rule.get("type", "any")
        if kind not in _TYPE_TABLE:
            problems.append(
                f"{owner}.{name}: unknown type {kind!r}; expected one of {sorted(_TYPE_TABLE)}"
            )
        if "enum" in rule and not isinstance(rule["enum"], list):
            problems.append(f"{owner}.{name}: 'enum' must be a list")
        if "path" in rule:
            if not isinstance(rule["path"], bool):
                problems.append(f"{owner}.{name}: 'path' must be a boolean")
            elif rule["path"] and rule.get("type", "any") != "str":
                problems.append(
                    f"{owner}.{name}: declares path: true but its type is "
                    f"{rule.get('type', 'any')!r}, not 'str'"
                )
        elif rule.get("type") == "str" and looks_like_a_path(name):
            problems.append(
                f"{owner}.{name}: the name looks like a filesystem path but the "
                "parameter does not declare path: true, so path confinement "
                "would not inspect it"
            )
        for bound in ("min", "max"):
            if bound in rule and not isinstance(rule[bound], (int, float)):
                problems.append(f"{owner}.{name}: {bound!r} must be a number")
    return problems


def validate(args: Mapping[str, Any], spec: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Return a list of problems; an empty list means the call is well-formed."""
    problems: list[str] = []
    args = dict(args or {})
    for name, rule in spec.items():
        rule = rule or {}
        if name in args and args[name] is None:
            args.pop(name)
        if name not in args:
            if rule.get("required"):
                problems.append(f"missing required parameter {name!r}")
            continue
        value = args[name]
        expected = _TYPE_TABLE.get(rule.get("type", "any"), (object,))
        if rule.get("type") == "int" and isinstance(value, bool):
            problems.append(f"parameter {name!r} must be an int, got bool")
        elif not isinstance(value, expected):
            problems.append(
                f"parameter {name!r} must be {rule.get('type', 'any')}, "
                f"got {type(value).__name__}"
            )
            continue
        if "enum" in rule and value not in rule["enum"]:
            problems.append(f"parameter {name!r} must be one of {rule['enum']}, got {value!r}")
        if "min" in rule and isinstance(value, (int, float)) and value < rule["min"]:
            problems.append(f"parameter {name!r} must be >= {rule['min']}, got {value!r}")
        if "max" in rule and isinstance(value, (int, float)) and value > rule["max"]:
            problems.append(f"parameter {name!r} must be <= {rule['max']}, got {value!r}")
    for name in args:
        if name not in spec:
            problems.append(f"unexpected parameter {name!r}")
    return problems


def coerce(args: Mapping[str, Any], spec: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Fill in defaults, then raise :class:`SchemaError` if the call is invalid."""
    filled = {k: v for k, v in dict(args or {}).items() if v is not None}
    for name, rule in spec.items():
        rule = rule or {}
        if name not in filled and "default" in rule:
            filled[name] = rule["default"]
    problems = validate(filled, spec)
    if problems:
        raise SchemaError("; ".join(problems))
    return filled
