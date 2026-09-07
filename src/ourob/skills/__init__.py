"""The skill layer: contracts, discovery, dispatch."""

from .base import SKILL_ATTR, Skill, SkillContext, SkillSpec, skill
from .registry import BUILTIN_PACKAGE, CONTRIB_DIR, SkillRegistry

__all__ = [
    "BUILTIN_PACKAGE",
    "CONTRIB_DIR",
    "SKILL_ATTR",
    "Skill",
    "SkillContext",
    "SkillRegistry",
    "SkillSpec",
    "skill",
]
