"""Skills: the registry, discovery (including self-authored skills) and dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from ourob.errors import SkillError, SkillNotFound
from ourob.skills.base import Skill, SkillContext, skill
from ourob.skills.registry import CONTRIB_DIR, SkillRegistry
from ourob.state.model import Invocation

NEW_SKILL = '''\
"""A skill the runtime wrote into itself."""

from ourob.skills.base import Skill, SkillContext, skill
from ourob.state.model import SkillResult


@skill(
    "shout",
    title="Shout",
    description="Return the input in upper case. Added by the runtime to prove self-extension works.",
    params={"text": {"type": "str", "required": True}},
)
class Shout(Skill):
    def run(self, ctx: SkillContext, **kwargs) -> SkillResult:
        return SkillResult(ok=True, output=str(kwargs["text"]).upper(), data={"shouted": True})
'''

BROKEN_SKILL = '''\
"""Fails at import time, which discovery must survive."""

raise RuntimeError("this skill cannot even be imported")
'''

IMPORT_ERROR_SKILL = "this is not python at all (((\n"


# -- registry -------------------------------------------------------------


def test_builtin_skills_are_discovered(registry: SkillRegistry) -> None:
    for expected in ("read_file", "write_file", "edit_file", "run_verification", "finish"):
        assert registry.has(expected), expected
    assert registry.problems() == []


def test_unknown_skill_lookup_reports_the_catalogue(registry: SkillRegistry) -> None:
    with pytest.raises(SkillNotFound) as excinfo:
        registry.get("teleport")
    assert "read_file" in str(excinfo.value)


def test_dispatch_rejects_bad_arguments(registry: SkillRegistry, skill_ctx: SkillContext) -> None:
    result = registry.dispatch(Invocation(skill="read_file", args={}), skill_ctx)
    assert not result.ok
    assert "missing required" in (result.error or "")


def test_dispatch_rejects_unknown_parameters(registry: SkillRegistry, skill_ctx: SkillContext) -> None:
    result = registry.dispatch(
        Invocation(skill="read_file", args={"path": "README.md", "wat": 1}), skill_ctx
    )
    assert not result.ok
    assert "unexpected parameter" in (result.error or "")


def test_dispatch_of_an_unknown_skill_is_a_result_not_an_exception(
    registry: SkillRegistry, skill_ctx: SkillContext
) -> None:
    result = registry.dispatch(Invocation(skill="nope", args={}), skill_ctx)
    assert not result.ok
    assert "no skill named" in (result.error or "")


def test_catalogue_is_planner_ready(registry: SkillRegistry) -> None:
    catalogue = registry.catalogue()
    assert catalogue
    entry = next(c for c in catalogue if c["name"] == "write_file")
    assert entry["mutating"] is True
    assert set(entry["params"]) == {"path", "content"}
    assert entry["description"]


# -- self-extension -------------------------------------------------------


def test_a_skill_written_into_contrib_becomes_available(repo: Path) -> None:
    target = repo / CONTRIB_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / "shout.py").write_text(NEW_SKILL, encoding="utf-8")

    registry = SkillRegistry(repo)
    problems = registry.discover()
    assert problems == []
    assert registry.has("shout")

    ctx = SkillContext(repo=repo, services={})
    result = registry.dispatch(Invocation(skill="shout", args={"text": "hello"}), ctx)
    assert result.ok
    assert result.output == "HELLO"


def test_a_broken_contrib_skill_is_quarantined_not_fatal(repo: Path) -> None:
    target = repo / CONTRIB_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / "broken.py").write_text(BROKEN_SKILL, encoding="utf-8")

    registry = SkillRegistry(repo)
    problems = registry.discover()
    assert any("broken.py" in p and "import failed" in p for p in problems), problems
    assert registry.has("read_file")  # the rest of the runtime still works
    assert not registry.has("broken")


def test_an_unimportable_contrib_file_is_reported(repo: Path) -> None:
    target = repo / CONTRIB_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / "junk.py").write_text(IMPORT_ERROR_SKILL, encoding="utf-8")

    registry = SkillRegistry(repo)
    problems = registry.discover()
    assert any("junk.py" in p and "import failed" in p for p in problems)


def test_a_duplicate_skill_name_is_refused(repo: Path) -> None:
    target = repo / CONTRIB_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / "dupe.py").write_text(NEW_SKILL.replace('"shout"', '"read_file"'), encoding="utf-8")

    registry = SkillRegistry(repo)
    problems = registry.discover()
    assert any("duplicate skill name" in p for p in problems)
    # the built-in wins
    assert "builtin" in registry._modules["read_file"]


def test_registration_requires_a_spec() -> None:
    class Naked(Skill):
        def run(self, ctx, **kwargs):  # pragma: no cover
            raise NotImplementedError

    registry = SkillRegistry(Path("."))
    with pytest.raises(SkillError):
        registry.register(Naked)  # type: ignore[arg-type]


def test_decorator_rejects_non_skills() -> None:
    with pytest.raises(TypeError):

        @skill("nope", description="x")
        class NotASkill:  # type: ignore[misc]
            pass


def test_skill_crashes_are_reported_not_raised(skill_ctx: SkillContext) -> None:
    @skill("crashy", description="raises on purpose")
    class Crashy(Skill):
        def run(self, ctx, **kwargs):
            raise ValueError("kaboom")

    registry = SkillRegistry(skill_ctx.repo)
    registry.register(Crashy)
    result = registry.dispatch(Invocation(skill="crashy", args={}), skill_ctx)
    assert not result.ok
    assert "kaboom" in (result.error or "")


def test_expected_skill_errors_carry_their_message(skill_ctx: SkillContext) -> None:
    @skill("grumpy", description="raises SkillError")
    class Grumpy(Skill):
        def run(self, ctx, **kwargs):
            raise SkillError("not today")

    registry = SkillRegistry(skill_ctx.repo)
    registry.register(Grumpy)
    result = registry.dispatch(Invocation(skill="grumpy", args={}), skill_ctx)
    assert not result.ok
    assert result.error == "not today"


# -- file skills ----------------------------------------------------------


def test_write_then_read_round_trip(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    written = registry.dispatch(
        Invocation(skill="write_file", args={"path": "scratch/a.txt", "content": "hello"}), skill_ctx
    )
    assert written.ok
    assert written.data["created"] is True
    read = registry.dispatch(Invocation(skill="read_file", args={"path": "scratch/a.txt"}), skill_ctx)
    assert read.ok
    assert read.output == "hello"


def test_write_overwrites_and_records_the_previous_hash(
    skill_ctx: SkillContext, registry: SkillRegistry
) -> None:
    registry.dispatch(Invocation(skill="write_file", args={"path": "a.txt", "content": "one"}), skill_ctx)
    second = registry.dispatch(
        Invocation(skill="write_file", args={"path": "a.txt", "content": "two"}), skill_ctx
    )
    assert second.data["created"] is False
    assert second.data["previous_sha256"]


def test_read_a_missing_file_is_an_error(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    result = registry.dispatch(Invocation(skill="read_file", args={"path": "nope.txt"}), skill_ctx)
    assert not result.ok
    assert "not a file" in (result.error or "")


def test_skills_cannot_leave_the_repository(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    result = registry.dispatch(
        Invocation(skill="write_file", args={"path": "../escaped.txt", "content": "x"}), skill_ctx
    )
    assert not result.ok
    assert "outside" in (result.error or "")


def test_edit_requires_a_unique_match(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    registry.dispatch(
        Invocation(skill="write_file", args={"path": "b.txt", "content": "aa\naa\n"}), skill_ctx
    )
    ambiguous = registry.dispatch(
        Invocation(skill="edit_file", args={"path": "b.txt", "old_text": "aa", "new_text": "bb"}),
        skill_ctx,
    )
    assert not ambiguous.ok
    assert "matches 2 times" in (ambiguous.error or "")

    ok = registry.dispatch(
        Invocation(
            skill="edit_file",
            args={"path": "b.txt", "old_text": "aa", "new_text": "bb", "occurrences": 2},
        ),
        skill_ctx,
    )
    assert ok.ok
    after = registry.dispatch(Invocation(skill="read_file", args={"path": "b.txt"}), skill_ctx)
    assert after.output == "bb\nbb\n"


def test_edit_refuses_an_absent_match(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    registry.dispatch(Invocation(skill="write_file", args={"path": "c.txt", "content": "x"}), skill_ctx)
    result = registry.dispatch(
        Invocation(skill="edit_file", args={"path": "c.txt", "old_text": "y", "new_text": "z"}),
        skill_ctx,
    )
    assert not result.ok
    assert "not found" in (result.error or "")


def test_delete_file(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    registry.dispatch(Invocation(skill="write_file", args={"path": "d.txt", "content": "x"}), skill_ctx)
    assert registry.dispatch(Invocation(skill="delete_file", args={"path": "d.txt"}), skill_ctx).ok
    assert not (skill_ctx.repo / "d.txt").exists()


def test_list_dir_and_recursive_listing(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    flat = registry.dispatch(Invocation(skill="list_dir", args={"path": "."}), skill_ctx)
    assert flat.ok
    assert any(e["path"] == "ourob.toml" for e in flat.data["entries"])
    deep = registry.dispatch(
        Invocation(skill="list_dir", args={"path": "src", "recursive": True}), skill_ctx
    )
    assert deep.ok
    assert any(e["path"].startswith("src/ourob/") for e in deep.data["entries"])


def test_grep_finds_and_reports_no_matches(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    hit = registry.dispatch(
        Invocation(skill="grep", args={"pattern": "def main", "path": "src"}), skill_ctx
    )
    assert hit.ok and hit.data["count"] > 0
    miss = registry.dispatch(
        Invocation(skill="grep", args={"pattern": "zzzz_no_such_token"}), skill_ctx
    )
    assert miss.ok and miss.data["count"] == 0


def test_grep_rejects_a_bad_pattern(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    result = registry.dispatch(Invocation(skill="grep", args={"pattern": "("}), skill_ctx)
    assert not result.ok
    assert "regular expression" in (result.error or "")


# -- execution skills -----------------------------------------------------


def test_run_python_executes_with_the_repo_on_the_path(
    skill_ctx: SkillContext, registry: SkillRegistry
) -> None:
    result = registry.dispatch(
        Invocation(skill="run_python", args={"code": "import ourob; print(ourob.__version__)"}),
        skill_ctx,
    )
    assert result.ok
    assert "0.1.0" in result.output


def test_run_python_reports_failures(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    result = registry.dispatch(
        Invocation(skill="run_python", args={"code": "raise SystemExit(3)"}), skill_ctx
    )
    assert not result.ok
    assert result.data["exit_code"] == 3


def test_run_python_honours_timeouts(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    result = registry.dispatch(
        Invocation(
            skill="run_python",
            args={"code": "import time; time.sleep(30)", "timeout": 1},
        ),
        skill_ctx,
    )
    assert not result.ok
    assert result.data["timed_out"] is True


def test_run_command_enforces_the_allowlist(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    blocked = registry.dispatch(
        Invocation(skill="run_command", args={"argv": ["bash", "-c", "ls"]}), skill_ctx
    )
    assert not blocked.ok
    assert "allowlist" in (blocked.error or "")

    allowed = registry.dispatch(
        Invocation(skill="run_command", args={"argv": ["python", "-c", "print(1+1)"]}), skill_ctx
    )
    assert allowed.ok
    assert "2" in allowed.output


def test_run_command_rejects_shell_metacharacters(
    skill_ctx: SkillContext, registry: SkillRegistry
) -> None:
    result = registry.dispatch(
        Invocation(skill="run_command", args={"argv": ["python", "-c", "print(1); print(2)"]}),
        skill_ctx,
    )
    assert not result.ok
    assert "metacharacter" in (result.error or "")


def test_run_command_rejects_empty_argv(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    result = registry.dispatch(Invocation(skill="run_command", args={"argv": []}), skill_ctx)
    assert not result.ok


# -- introspection skills -------------------------------------------------


def test_list_skills_includes_contrib(repo: Path) -> None:
    target = repo / CONTRIB_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / "shout.py").write_text(NEW_SKILL, encoding="utf-8")
    registry = SkillRegistry(repo)
    registry.discover()
    from ourob.config import Config
    from ourob.state.store import StateStore

    ctx = SkillContext(
        repo=repo, store=StateStore(repo), services={"registry": registry, "config": Config.load(repo)}
    )
    result = registry.dispatch(Invocation(skill="list_skills", args={}), ctx)
    assert result.ok
    assert "shout" in result.output


def test_read_manifest_reports_drift(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    result = registry.dispatch(Invocation(skill="read_manifest", args={}), skill_ctx)
    assert result.ok
    assert result.data["files"] > 0
    assert result.data["drift"]["clean"] is True

    registry.dispatch(Invocation(skill="write_file", args={"path": "NEW.md", "content": "x"}), skill_ctx)
    after = registry.dispatch(Invocation(skill="read_manifest", args={}), skill_ctx)
    assert "NEW.md" in after.data["drift"]["added"]


def test_read_manifest_without_a_lock_is_an_error(tmp_path: Path) -> None:
    from ourob.config import Config
    from ourob.state.store import StateStore

    (tmp_path / "ourob.toml").write_text("[policy]\nprotected = ['x']\n", encoding="utf-8")
    registry = SkillRegistry(tmp_path)
    registry.discover()
    ctx = SkillContext(
        repo=tmp_path,
        store=StateStore(tmp_path),
        services={"registry": registry, "config": Config.load(tmp_path)},
    )
    result = registry.dispatch(Invocation(skill="read_manifest", args={}), ctx)
    assert not result.ok
    assert "unanchored" in (result.error or "")


def test_finish_is_terminal(skill_ctx: SkillContext, registry: SkillRegistry) -> None:
    result = registry.dispatch(
        Invocation(skill="finish", args={"summary": "all done", "success": True}), skill_ctx
    )
    assert result.ok
    assert result.data["terminal"] is True


def test_propose_amendment_skill_records_and_authorises(
    repo: Path, store, config
) -> None:
    from ourob.bootstrap.amend import AmendmentLedger

    registry = SkillRegistry(repo)
    registry.discover()
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    ctx = SkillContext(
        repo=repo,
        run_id="run-x",
        store=store,
        services={"config": config, "registry": registry, "ledger": ledger, "amended_paths": []},
    )
    result = registry.dispatch(
        Invocation(
            skill="propose_amendment",
            args={"paths": ["src/ourob/policies/"], "rationale": "need to tighten a rule"},
        ),
        ctx,
    )
    assert result.ok
    assert ledger.authorising("src/ourob/policies/rules.py") is not None
    assert ctx.services["amended_paths"] == ["src/ourob/policies/"]
    assert any(e["kind"] == "amendment.proposed" for e in store.journal("run-x").events())
