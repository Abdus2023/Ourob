"""Skill contracts.

A skill is the unit of capability.  It declares its name, a description a
planner can reason about, a parameter schema, and whether it mutates the
repository.  Everything the runtime can *do* -- including the things it does to
itself -- is a skill, which means every action passes through the same policy
and journal path.

Skills are discovered by scanning packages, not by a hard-coded table, so a skill
the runtime writes into ``skills/contrib/`` is live on the next discovery pass.
That is what makes self-extension mechanical rather than aspirational.
"""

from __future__ import annotations

import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..errors import SkillError
from ..state.model import SkillResult

SKILL_ATTR = "__ourob_skills__"


@dataclass(frozen=True)
class SkillSpec:
    name: str
    title: str
    description: str
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    mutating: bool = False
    examples: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "params": {k: dict(v) for k, v in self.params.items()},
            "mutating": self.mutating,
            "examples": list(self.examples),
        }


@dataclass
class SkillContext:
    """Everything a skill is allowed to know about the run it is part of."""

    repo: Path
    run_id: str = ""
    step: int = 0
    store: Any | None = None
    services: dict[str, Any] = field(default_factory=dict)
    logger: Callable[[str], None] | None = None

    def log(self, message: str) -> None:
        if self.logger is not None:
            self.logger(message)

    def service(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError as exc:
            raise SkillError(f"skill service {name!r} is not available") from exc

    def path(self, relpath: str) -> Path:
        """Repository-confined path resolution.  Every file skill uses this."""
        from ..fsx import confine

        return confine(self.repo, relpath)


class Skill(ABC):
    """Base class for capabilities."""

    spec: ClassVar[SkillSpec]

    @abstractmethod
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        """Execute the skill.  Raise :class:`SkillError` for expected failures."""

    @property
    def name(self) -> str:
        return self.spec.name

    def timed(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        started = time.time()
        try:
            result = self.run(ctx, **kwargs)
        except SkillError as exc:
            result = SkillResult(ok=False, error=str(exc), output=str(exc))
        except Exception as exc:  # unexpected: recorded, never fatal to the loop
            result = SkillResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                output=f"skill {self.name} crashed: {type(exc).__name__}: {exc}",
            )
        result.duration_ms = int((time.time() - started) * 1000)
        return result


def skill(
    name: str,
    *,
    title: str = "",
    description: str = "",
    params: dict[str, dict[str, Any]] | None = None,
    mutating: bool = False,
    examples: tuple[str, ...] = (),
) -> Callable[[type[Skill]], type[Skill]]:
    """Declare a skill.  Registers the class on its own module for discovery."""

    def decorator(cls: type[Skill]) -> type[Skill]:
        if not issubclass(cls, Skill):
            raise TypeError(f"@skill can only decorate Skill subclasses, got {cls!r}")
        docstring = (cls.__doc__ or "").strip()
        first_line = docstring.splitlines()[0].strip() if docstring else ""
        cls.spec = SkillSpec(
            name=name,
            title=title or name,
            description=description or first_line or name,
            params=params or {},
            mutating=mutating,
            examples=examples,
        )
        module = sys.modules[cls.__module__]
        bucket: list[type[Skill]] = getattr(module, SKILL_ATTR, [])
        bucket.append(cls)
        setattr(module, SKILL_ATTR, bucket)
        return cls

    return decorator
