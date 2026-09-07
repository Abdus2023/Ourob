"""An optional language-model planner.

This is the only place in the runtime that talks to the outside world, and it is
strictly optional: if ``httpx`` is missing or ``OUROB_API_KEY`` is unset the
kernel simply will not offer this planner.  The safety machinery does not change
based on which planner is in use -- an LLM's invocation passes through exactly
the same policies, journal and gates as a scripted one.

Protocol: the model is shown the goal, the skill catalogue with parameter
schemas, the protected paths, and the transcript so far.  It must reply with a
single JSON object::

    {"skill": "write_file", "args": {...}, "rationale": "..."}

or ``{"skill": null}`` to end the run.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..errors import ConfigError
from ..state.model import Invocation
from .base import Planner, RuntimeView

SYSTEM_PROMPT = """\
You are the planning component of ourob, an autonomous engineering runtime that
lives inside the repository it is allowed to modify. You choose exactly one
skill invocation per turn.

Rules you are expected to work within:
- Every path you pass must be relative to the repository root and must stay inside it.
- Protected paths cannot be written without an amendment; if you need one, use the
  propose_amendment skill and explain why in the rationale.
- The runtime's history under .ourob/ is append-only; do not try to write there.
- After changing anything in the runtime, run run_verification before finishing.
- Finish by calling the `finish` skill with an honest summary.

Reply with ONLY a JSON object: {"skill": <name|null>, "args": {...}, "rationale": "..."}
"""


def _render_view(view: RuntimeView) -> str:
    lines = [
        f"GOAL: {view.goal}",
        f"RUN: {view.run_id}  step {view.step_index}  budget remaining {view.budget_remaining}",
        "",
        "PROTECTED PATHS (write requires an amendment):",
        *(  [f"  - {p}" for p in view.protected_paths] or ["  (none)"]  ),
        "",
        "SKILLS:",
    ]
    for entry in view.skills:
        lines.append(f"  {entry['name']} -- {entry['description']}")
        for pname, rule in entry.get("params", {}).items():
            bits = [rule.get("type", "any")]
            if rule.get("required"):
                bits.append("required")
            if "default" in rule:
                bits.append(f"default={rule['default']!r}")
            if "enum" in rule:
                bits.append(f"one of {rule['enum']}")
            if rule.get("desc"):
                bits.append(rule["desc"])
            lines.append(f"      {pname}: {', '.join(str(b) for b in bits)}")
    lines += ["", "TRANSCRIPT:", "  " + view.transcript().replace("\n", "\n  ")]
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"model reply was not JSON: {exc}\n---\n{text[:500]}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"model reply was not an object: {text[:200]}")
    return data


class OpenAIPlanner(Planner):
    """Talks to any OpenAI-compatible ``/chat/completions`` endpoint."""

    name = "llm"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1500,
        timeout: float = 120.0,
    ) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ConfigError(
                "the LLM planner needs httpx; install with `pip install ourob[llm]`"
            ) from exc
        self.api_key = api_key or os.environ.get("OUROB_API_KEY", "")
        if not self.api_key:
            raise ConfigError("OUROB_API_KEY is not set")
        self.base_url = (base_url or os.environ.get("OUROB_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("OUROB_MODEL", "gpt-4o-mini")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    def next_action(self, view: RuntimeView) -> Invocation | None:
        import httpx

        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _render_view(view)},
            ],
        }
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"] or ""
        data = _extract_json(content)
        skill_name = data.get("skill")
        if skill_name in (None, "", "null", "none"):
            return None
        return Invocation(
            skill=str(skill_name),
            args=dict(data.get("args") or {}),
            rationale=str(data.get("rationale", "")),
        )

    def describe(self) -> str:
        return f"llm[{self.model} @ {self.base_url}]"
