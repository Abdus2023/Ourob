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
- Protected paths cannot be written without a currently valid, separately recorded
  operator authorization. propose_amendment creates only a proposal; it never grants
  authority. Only an operator may use the standalone authorize command.
- The runtime's history under .ourob/ is append-only; do not try to write there.
- Skill discovery is a startup snapshot. New or changed contrib files are pending;
  do not invoke them in this run. Verify and promote, then start a fresh runtime.
- After changing anything in the runtime, run run_verification before finishing.
- Finish by calling the `finish` skill with an honest summary.

Reply with ONLY a JSON object: {"skill": <name|null>, "args": {...}, "rationale": "..."}
"""


def _render_view(view: RuntimeView) -> str:
    lines = [
        f"GOAL: {view.goal}",
        f"RUN: {view.run_id}  step {view.step_index}  budget remaining {view.budget_remaining}",
        "",
        "PROTECTED PATHS (write requires a valid separate operator grant):",
        *([f"  - {p}" for p in view.protected_paths] or ["  (none)"]),
    ]
    discovery = view.skill_discovery
    lines.extend(
        [
            "",
            "SKILL DISCOVERY (startup snapshot; do not invoke pending files):",
            f"  complete: {discovery.get('complete', 'unknown')}",
        ]
    )
    for change, paths in discovery.get("pending", {}).items():
        if paths:
            lines.append(f"  {change}: {', '.join(paths)}")
    lines += ["", "SKILLS:"]
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


def _content_of(body: Any) -> str:
    """Pull the assistant message out of a chat-completions response.

    Anything that is not the documented shape is a :class:`ConfigError`, not a
    ``KeyError``: the endpoint is untrusted, and "the model gateway replied with
    something we did not expect" is a configuration problem worth naming.
    """
    if not isinstance(body, dict):
        raise ConfigError(f"gateway replied with a {type(body).__name__}, not an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ConfigError(f"gateway reply has no choices: {str(body)[:300]}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict) or "content" not in message:
        raise ConfigError(f"gateway reply has no message content: {str(body)[:300]}")
    return str(message.get("content") or "")


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the action object out of a model reply.

    Two-stage on purpose.  If the reply (or its fenced block) *is* valid JSON,
    that is taken as the whole answer and anything but an object is a protocol
    violation -- silently unwrapping ``[{...}]`` into ``{...}`` would hide a model
    that is not following the contract.  Only when the reply is not valid JSON on
    its own do we fall back to lifting the first object out of surrounding prose.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace is None:
            raise ConfigError(
                f"model reply contained no JSON object:\n---\n{candidate[:500]}"
            ) from None
        try:
            data = json.loads(brace.group(0))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"model reply was not JSON: {exc}\n---\n{brace.group(0)[:500]}"
            ) from exc

    if not isinstance(data, dict):
        raise ConfigError(f"model reply was not an object: {str(data)[:200]}")
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
        transport: Any | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ConfigError(
                "the LLM planner needs httpx; install with `pip install ourob[llm]`"
            ) from exc
        self.api_key = api_key or os.environ.get("OUROB_API_KEY", "")
        if not self.api_key:
            raise ConfigError("OUROB_API_KEY is not set")
        self.base_url = (
            base_url or os.environ.get("OUROB_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("OUROB_MODEL", "gpt-4o-mini")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        # ``transport`` exists so the planner can be exercised against a fake
        # gateway; it is not a configuration knob and is never read from the env.
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAIPlanner:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def next_action(self, view: RuntimeView) -> Invocation | None:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _render_view(view)},
            ],
        }
        response = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise ConfigError(f"gateway reply was not JSON: {exc}") from exc
        data = _extract_json(_content_of(body))
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
