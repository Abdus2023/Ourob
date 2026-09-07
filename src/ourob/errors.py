"""Exception hierarchy for the runtime.

Every failure the runtime can produce is a subclass of :class:`OurobError`, so a
caller can catch the whole surface of the system with one clause.  The
subclasses exist so that *policy* failures, *verification* failures and plain
bugs are distinguishable in the journal: they mean very different things when a
machine is allowed to rewrite itself.
"""

from __future__ import annotations


class OurobError(Exception):
    """Base class for every error raised by the runtime."""


class ConfigError(OurobError):
    """``ourob.toml`` is missing, malformed, or internally inconsistent."""


class PathEscapeError(OurobError):
    """A skill tried to touch a path outside the repository root."""


class SkillError(OurobError):
    """A skill failed for a reason that is the skill's own fault."""


class SkillNotFound(SkillError):
    """The planner asked for a skill that is not in the registry."""


class SchemaError(SkillError):
    """Skill arguments did not satisfy the declared parameter schema."""


class PolicyViolation(OurobError):
    """A policy denied an invocation."""

    def __init__(self, message: str, *, policy: str = "unknown") -> None:
        super().__init__(message)
        self.policy = policy


class AmendmentRequired(PolicyViolation):
    """A protected path was touched without a ratified amendment."""


class VerificationFailure(OurobError):
    """A blocking gate failed."""


class StateError(OurobError):
    """The journal or state directory is unreadable or inconsistent."""


class BootstrapError(OurobError):
    """The repository could not be trusted enough to boot from."""
