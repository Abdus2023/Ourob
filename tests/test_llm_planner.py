"""The LLM planner: the only component that turns untrusted text into an action.

Everything here runs offline against ``httpx.MockTransport``. No network, no key,
no model -- but the full path from a raw gateway response to an
:class:`Invocation`, including every way the reply can be wrong.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ourob.errors import ConfigError
from ourob.planner.base import Observation, RuntimeView
from ourob.planner.llm import (
    SYSTEM_PROMPT,
    OpenAIPlanner,
    _content_of,
    _extract_json,
    _render_view,
)

VIEW = RuntimeView(
    goal="Add a rot13 skill to the runtime",
    run_id="run-test",
    step_index=2,
    budget_remaining=7,
    protected_paths=["ourob.toml", "src/ourob/policies/"],
    skills=[
        {
            "name": "write_file",
            "description": "Create or overwrite a file.",
            "params": {
                "path": {"type": "str", "required": True, "desc": "repository-relative path"},
                "content": {"type": "str", "required": True},
            },
        },
        {
            "name": "finish",
            "description": "Declare the run complete.",
            "params": {"success": {"type": "bool", "default": True, "enum": [True, False]}},
        },
    ],
    history=[
        Observation(index=0, skill="read_manifest", args={}, ok=True, output="no drift"),
        Observation(index=1, skill="write_file", args={"path": "a.py"}, ok=False, error="denied"),
    ],
)


def reply(content: Any, status: int = 200) -> httpx.Response:
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return httpx.Response(status, json=body)


def planner_for(handler, **kwargs) -> OpenAIPlanner:
    return OpenAIPlanner(api_key="test-key", transport=httpx.MockTransport(handler), **kwargs)


# -- _extract_json --------------------------------------------------------


def test_extracts_a_bare_object() -> None:
    assert _extract_json('{"skill": "read_file"}') == {"skill": "read_file"}


def test_extracts_from_a_fenced_json_block() -> None:
    text = '```json\n{"skill": "finish", "args": {"success": true}}\n```'
    assert _extract_json(text) == {"skill": "finish", "args": {"success": True}}


def test_extracts_from_a_fence_without_a_language() -> None:
    assert _extract_json('```\n{"skill": null}\n```') == {"skill": None}


def test_extracts_an_object_wrapped_in_prose() -> None:
    text = 'Sure! Here you go:\n{"skill": "grep", "args": {"pattern": "x"}}\nHope that helps.'
    assert _extract_json(text)["skill"] == "grep"


def test_surrounding_whitespace_is_ignored() -> None:
    assert _extract_json('\n\n  {"skill": "list_dir"}  \n\n') == {"skill": "list_dir"}


def test_a_reply_that_is_neither_json_nor_prose_with_json_is_a_config_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _extract_json("I think we should probably read the manifest first.")
    assert "no JSON object" in str(excinfo.value)


def test_a_json_array_is_rejected() -> None:
    """A valid-JSON reply that is not an object is a protocol violation, not
    something to silently unwrap."""
    with pytest.raises(ConfigError) as excinfo:
        _extract_json('[{"skill": "read_file"}]')
    assert "not an object" in str(excinfo.value)


def test_a_nested_object_survives_both_stages() -> None:
    action = {"skill": "write_file", "args": {"path": "a.py", "content": "x"}}
    assert _extract_json(json.dumps(action)) == action
    assert _extract_json(f"```json\n{json.dumps(action)}\n```") == action
    assert _extract_json(f"Here:\n{json.dumps(action)}\nDone.") == action


def test_a_json_scalar_reply_is_rejected() -> None:
    with pytest.raises(ConfigError):
        _extract_json('"read_file"')


def test_a_reply_with_no_object_at_all_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _extract_json("let me think about that for a moment")
    assert "no JSON object" in str(excinfo.value)


def test_a_truncated_object_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        _extract_json('{"skill": "read_file", "args": {')


def test_the_error_message_carries_the_offending_text() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _extract_json("definitely not json at all")
    assert "definitely not json" in str(excinfo.value)


def test_a_broken_object_inside_prose_reports_the_parse_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _extract_json('sure, here: {"skill": "read_file", broken}')
    assert "was not JSON" in str(excinfo.value)


# -- _content_of ----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": "nope"},
        [],
        "a string",
        None,
    ],
)
def test_malformed_gateway_shapes_are_config_errors(body: Any) -> None:
    with pytest.raises(ConfigError):
        _content_of(body)


def test_content_is_read_from_the_first_choice() -> None:
    body = {"choices": [{"message": {"content": "hi"}}, {"message": {"content": "ignored"}}]}
    assert _content_of(body) == "hi"


def test_null_content_becomes_an_empty_string() -> None:
    assert _content_of({"choices": [{"message": {"content": None}}]}) == ""


# -- _render_view ---------------------------------------------------------


def test_the_view_states_the_goal_and_the_budget() -> None:
    text = _render_view(VIEW)
    assert "GOAL: Add a rot13 skill to the runtime" in text
    assert "RUN: run-test  step 2  budget remaining 7" in text


def test_the_view_lists_protected_paths() -> None:
    text = _render_view(VIEW)
    assert "  - ourob.toml" in text
    assert "  - src/ourob/policies/" in text


def test_an_empty_protected_list_is_stated_explicitly() -> None:
    view = RuntimeView(goal="g", run_id="r", step_index=0, budget_remaining=1)
    assert "  (none)" in _render_view(view)


def test_the_view_renders_every_parameter_attribute() -> None:
    text = _render_view(VIEW)
    assert "write_file -- Create or overwrite a file." in text
    assert "path: str, required, repository-relative path" in text
    assert "content: str, required" in text
    assert "success: bool, default=True, one of [True, False]" in text


def test_a_skill_without_params_renders_without_children() -> None:
    view = RuntimeView(
        goal="g",
        run_id="r",
        step_index=0,
        budget_remaining=1,
        skills=[{"name": "finish", "description": "stop", "params": {}}],
    )
    text = _render_view(view)
    assert "  finish -- stop" in text


def test_the_transcript_is_included() -> None:
    text = _render_view(VIEW)
    assert "TRANSCRIPT:" in text
    assert "#0 read_manifest -> ok" in text
    assert "#1 write_file -> error" in text


def test_an_empty_transcript_says_so() -> None:
    view = RuntimeView(goal="g", run_id="r", step_index=0, budget_remaining=1)
    assert "(no steps taken yet)" in _render_view(view)


# -- construction ---------------------------------------------------------


def test_an_api_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROB_API_KEY", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        OpenAIPlanner()
    assert "OUROB_API_KEY" in str(excinfo.value)


def test_the_key_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROB_API_KEY", "from-env")
    monkeypatch.delenv("OUROB_MODEL", raising=False)
    monkeypatch.delenv("OUROB_BASE_URL", raising=False)
    planner = OpenAIPlanner()
    assert planner.api_key == "from-env"
    assert planner.model == "gpt-4o-mini"
    assert planner.base_url == "https://api.openai.com/v1"


def test_explicit_arguments_win_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROB_API_KEY", "from-env")
    monkeypatch.setenv("OUROB_MODEL", "env-model")
    monkeypatch.setenv("OUROB_BASE_URL", "https://env.example/v1")
    planner = OpenAIPlanner(
        api_key="explicit", model="explicit-model", base_url="https://explicit.example/v1/"
    )
    assert planner.api_key == "explicit"
    assert planner.model == "explicit-model"
    assert planner.base_url == "https://explicit.example/v1"  # trailing slash stripped


def test_describe_names_the_model_and_endpoint() -> None:
    planner = OpenAIPlanner(api_key="k", model="m", base_url="https://x.test/v1")
    assert planner.describe() == "llm[m @ https://x.test/v1]"


# -- next_action ----------------------------------------------------------


def test_a_well_formed_reply_becomes_an_invocation() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        seen["payload"] = json.loads(request.content)
        return reply('{"skill": "write_file", "args": {"path": "a.py", "content": "x"},'
                     ' "rationale": "because"}')

    with planner_for(handler) as planner:
        action = planner.next_action(VIEW)

    assert action is not None
    assert action.skill == "write_file"
    assert action.args == {"path": "a.py", "content": "x"}
    assert action.rationale == "because"


def test_the_request_is_addressed_and_authorised_correctly() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return reply('{"skill": null}')

    with planner_for(handler, model="test-model", temperature=0.1, max_tokens=42) as planner:
        planner.next_action(VIEW)

    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["payload"]["model"] == "test-model"
    assert seen["payload"]["temperature"] == 0.1
    assert seen["payload"]["max_tokens"] == 42


def test_the_system_prompt_and_the_view_are_both_sent() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return reply('{"skill": null}')

    with planner_for(handler) as planner:
        planner.next_action(VIEW)

    messages = seen["payload"]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == SYSTEM_PROMPT
    assert "GOAL: Add a rot13 skill to the runtime" in messages[1]["content"]


def test_a_fenced_reply_is_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply('```json\n{"skill": "grep", "args": {"pattern": "x"}}\n```')

    with planner_for(handler) as planner:
        action = planner.next_action(VIEW)

    assert action is not None and action.skill == "grep"


@pytest.mark.parametrize("terminator", [None, "", "null", "none"])
def test_the_model_can_end_the_run(terminator: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply(json.dumps({"skill": terminator}))

    with planner_for(handler) as planner:
        assert planner.next_action(VIEW) is None


def test_missing_args_and_rationale_default_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply('{"skill": "list_dir"}')

    with planner_for(handler) as planner:
        action = planner.next_action(VIEW)

    assert action is not None
    assert action.args == {}
    assert action.rationale == ""


def test_a_non_string_skill_name_is_coerced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply('{"skill": 17, "args": null}')

    with planner_for(handler) as planner:
        action = planner.next_action(VIEW)

    assert action is not None and action.skill == "17" and action.args == {}


def test_an_http_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    with planner_for(handler) as planner, pytest.raises(httpx.HTTPStatusError) as excinfo:
        planner.next_action(VIEW)
    assert excinfo.value.response.status_code == 503


def test_a_transport_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with planner_for(handler) as planner, pytest.raises(httpx.ConnectError):
        planner.next_action(VIEW)


def test_a_timeout_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with planner_for(handler, timeout=0.01) as planner, pytest.raises(httpx.ReadTimeout):
        planner.next_action(VIEW)


def test_a_non_json_body_is_a_config_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error page</html>")

    with planner_for(handler) as planner, pytest.raises(ConfigError) as excinfo:
        planner.next_action(VIEW)
    assert "not JSON" in str(excinfo.value)


def test_an_empty_reply_is_a_config_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply("")

    with planner_for(handler) as planner, pytest.raises(ConfigError) as excinfo:
        planner.next_action(VIEW)
    assert "no JSON object" in str(excinfo.value)


def test_a_well_formed_reply_with_the_wrong_shape_is_a_config_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "rate limited"}})

    with planner_for(handler) as planner, pytest.raises(ConfigError) as excinfo:
        planner.next_action(VIEW)
    assert "no choices" in str(excinfo.value)


def test_a_prose_only_reply_is_a_config_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply("I cannot help with that.")

    with planner_for(handler) as planner, pytest.raises(ConfigError):
        planner.next_action(VIEW)


def test_the_base_url_is_used_when_overridden() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return reply('{"skill": null}')

    with planner_for(handler, base_url="https://gateway.internal/v1/") as planner:
        planner.next_action(VIEW)

    assert seen["url"] == "https://gateway.internal/v1/chat/completions"


# -- integration with the kernel -----------------------------------------


def test_the_llm_planner_drives_the_kernel_like_any_other(repo) -> None:
    """Same loop, same policies, same journal -- the planner is interchangeable."""
    from ourob.kernel import Kernel
    from ourob.state.model import RunStatus

    scripted = iter(
        [
            reply('{"skill": "list_dir", "args": {"path": "src"}, "rationale": "look"}'),
            reply('{"skill": "write_file", "args": {"path": "../../escape.txt",'
                  ' "content": "x"}, "rationale": "try to escape"}'),
            reply('{"skill": "finish", "args": {"summary": "done", "success": true}}'),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(scripted)

    with planner_for(handler) as planner:
        outcome = Kernel(repo, verify_at_end=False).run("drive it with a model", planner)

    assert outcome.run.status is RunStatus.COMPLETED
    assert outcome.run.step_count == 3
    assert outcome.denied == 1
    denied = [s for s in outcome.run.steps if s.denied][0]
    assert any(d.policy == "path-confinement" for d in denied.decisions)
