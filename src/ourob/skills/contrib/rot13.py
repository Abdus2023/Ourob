"""A skill the runtime authored itself.

Written by ``plans/self_extend.json`` to demonstrate that a running ourob can add
a capability to the runtime it is made of, and that the addition is held to the
same standard as everything else: it has to lint, compile, import and pass the
tests before it is accepted.
"""

from typing import Any

from ourob.skills.base import Skill, SkillContext, skill
from ourob.state.model import SkillResult

ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
ROTATED = "nopqrstuvwxyzabcdefghijklmNOPQRSTUVWXYZABCDEFGHIJKLM"
TABLE = str.maketrans(ALPHABET, ROTATED)


@skill(
    "rot13",
    title="ROT13",
    description="Rotate the ASCII letters of a string by thirteen places. Self-authored skill.",
    params={"text": {"type": "str", "required": True, "desc": "text to rotate"}},
)
class Rot13(Skill):
    """ROT13 cipher, added by the runtime to itself."""

    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        text = str(kwargs["text"])
        return SkillResult(ok=True, output=text.translate(TABLE), data={"length": len(text)})
