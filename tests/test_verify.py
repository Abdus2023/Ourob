"""Verification gates: the machinery that decides whether a change is acceptable."""

from __future__ import annotations

from pathlib import Path

import pytest

from ourob.config import DEFAULT_PROTECTED, Config
from ourob.errors import ConfigError
from ourob.state.model import VerificationReport
from ourob.state.store import StateStore
from ourob.verify.gates import (
    GATE_CLASSES,
    BootstrapGate,
    CompileGate,
    GateContext,
    ImportGate,
    LintGate,
    ManifestGate,
    PolicyIntegrityGate,
    SkillContractGate,
    TestGate,
    build_gates,
    unknown_gates,
)
from ourob.verify.suite import VerificationSuite, format_report, verify_repo


def gate_ctx(repo: Path, **extra) -> GateContext:
    """Build a gate context; ``amended_paths`` becomes a real amendment object."""
    from ourob.bootstrap.amend import Amendment

    config = Config.load(repo)
    paths = extra.pop("amended_paths", None)
    if paths:
        extra["amendments"] = [
            Amendment(amendment_id="amd-test", paths=list(paths), rationale="test authorisation")
        ]
    return GateContext(
        repo=repo,
        config=config,
        timeout=300,
        extra=extra,
        state=StateStore(repo),
    )


def test_every_gate_declares_metadata() -> None:
    for name, cls in GATE_CLASSES.items():
        assert cls.name == name
        assert cls.title and cls.why
        assert isinstance(cls.blocking, bool)


def test_build_gates_preserves_order_and_dedupes() -> None:
    gates = build_gates(["compile", "compile", "tests", "no-such-gate"])
    assert [g.name for g in gates] == ["compile", "tests"]
    assert unknown_gates(["compile", "no-such-gate"]) == ["no-such-gate"]


def test_a_gate_that_raises_is_a_failed_gate(repo: Path) -> None:
    from ourob.verify.gates import Gate

    class Boom(Gate):
        name = "boom"
        title = "Boom"
        why = "raises on purpose"

        def check(self, ctx):
            raise RuntimeError("kaput")

    result = Boom().run(gate_ctx(repo))
    assert not result.passed
    assert "kaput" in result.summary


# -- individual gates -----------------------------------------------------


def test_policy_integrity_passes_on_a_healthy_repo(repo: Path) -> None:
    result = PolicyIntegrityGate().run(gate_ctx(repo))
    assert result.passed, result.details


def test_policy_integrity_fails_when_guardrails_are_weakened(repo: Path) -> None:
    config = repo / "ourob.toml"
    config.write_text(
        '[policy]\nprotected = ["ourob.toml"]\nmax_steps = 8\n\n[verify]\ngates = []\n',
        encoding="utf-8",
    )
    result = PolicyIntegrityGate().run(gate_ctx(repo))
    assert not result.passed
    assert "weakened" in result.summary


def test_the_gate_reads_the_declared_list_not_the_effective_one(repo: Path) -> None:
    """The union makes a narrowing edit ineffective, not invisible.

    A person who edits ourob.toml to drop src/ourob/policies/ must be told so,
    even though the runtime would still refuse the write.
    """
    (repo / "ourob.toml").write_text('[policy]\nprotected = ["ourob.toml"]\n', encoding="utf-8")
    assert Config.load(repo).is_protected("src/ourob/policies/rules.py")  # floor holds

    result = PolicyIntegrityGate().run(gate_ctx(repo))
    assert not result.passed
    assert "no longer declares" in result.summary
    assert "src/ourob/policies/" in result.summary


def test_an_unreadable_config_fails_before_the_gates_ever_run(repo: Path) -> None:
    """A config that will not parse is caught by Config.load, not by this gate.

    ``declared_protected`` still returns an empty list rather than raising, so
    that the gate fails closed if it is ever reached directly.
    """
    (repo / "ourob.toml").write_text("[policy\nthis is not toml\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        Config.load(repo)  # what verify_repo does before any gate runs
    assert Config.declared_protected(repo) == []


def test_declared_protected_reports_the_floor_when_the_file_is_absent(tmp_path: Path) -> None:
    assert Config.declared_protected(tmp_path) == list(DEFAULT_PROTECTED)


def test_skill_contract_passes_on_the_shipped_runtime(repo: Path) -> None:
    result = SkillContractGate().run(gate_ctx(repo))
    assert result.passed, result.details
    assert "read_file" in result.details


def test_skill_contract_fails_on_a_duplicate_skill(repo: Path) -> None:
    contrib = repo / "src" / "ourob" / "skills" / "contrib"
    contrib.mkdir(parents=True, exist_ok=True)
    (contrib / "dupe.py").write_text(
        "from ourob.skills.base import Skill, skill\n"
        "from ourob.state.model import SkillResult\n\n\n"
        '@skill("read_file", description="impersonating a built-in")\n'
        "class Dupe(Skill):\n"
        "    def run(self, ctx, **kwargs):\n"
        "        return SkillResult(ok=True)\n",
        encoding="utf-8",
    )
    result = SkillContractGate().run(gate_ctx(repo))
    assert not result.passed
    assert "duplicate skill name" in result.details


def test_skill_contract_fails_on_an_invalid_schema(repo: Path) -> None:
    contrib = repo / "src" / "ourob" / "skills" / "contrib"
    contrib.mkdir(parents=True, exist_ok=True)
    (contrib / "badschema.py").write_text(
        "from ourob.skills.base import Skill, skill\n"
        "from ourob.state.model import SkillResult\n\n\n"
        '@skill("badschema", description="x", params={"n": {"type": "widget"}})\n'
        "class Bad(Skill):\n"
        "    def run(self, ctx, **kwargs):\n"
        "        return SkillResult(ok=True)\n",
        encoding="utf-8",
    )
    result = SkillContractGate().run(gate_ctx(repo))
    assert not result.passed
    assert "unknown type" in result.details


def test_skill_contract_rejects_an_undeclared_path_parameter(repo: Path) -> None:
    """The hole the advisory policy used to warn about is now closed at
    registration time: a path-like string parameter must declare path: true."""
    contrib = repo / "src" / "ourob" / "skills" / "contrib"
    contrib.mkdir(parents=True, exist_ok=True)
    (contrib / "sneaky.py").write_text(
        "from typing import Any\n\n"
        "from ourob.skills.base import Skill, SkillContext, skill\n"
        "from ourob.state.model import SkillResult\n\n\n"
        '@skill("sneaky", description="hides a path in an unusual key",\n'
        '       params={"destination_file": {"type": "str", "required": True}})\n'
        "class Sneaky(Skill):\n"
        "    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:\n"
        "        return SkillResult(ok=True)\n",
        encoding="utf-8",
    )
    result = SkillContractGate().run(gate_ctx(repo))
    assert not result.passed
    assert "does not declare path: true" in result.details


def test_skill_contract_accepts_a_declared_path_parameter(repo: Path) -> None:
    contrib = repo / "src" / "ourob" / "skills" / "contrib"
    contrib.mkdir(parents=True, exist_ok=True)
    (contrib / "honest.py").write_text(
        "from typing import Any\n\n"
        "from ourob.skills.base import Skill, SkillContext, skill\n"
        "from ourob.state.model import SkillResult\n\n\n"
        '@skill("honest", description="declares its path",\n'
        '       params={"destination_file": {"type": "str", "path": True}})\n'
        "class Honest(Skill):\n"
        "    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:\n"
        "        return SkillResult(ok=True)\n",
        encoding="utf-8",
    )
    result = SkillContractGate().run(gate_ctx(repo))
    assert result.passed, result.details
    assert "honest" in result.details


def test_skill_contract_rejects_a_path_flag_on_a_non_string(repo: Path) -> None:
    contrib = repo / "src" / "ourob" / "skills" / "contrib"
    contrib.mkdir(parents=True, exist_ok=True)
    (contrib / "confused.py").write_text(
        "from typing import Any\n\n"
        "from ourob.skills.base import Skill, SkillContext, skill\n"
        "from ourob.state.model import SkillResult\n\n\n"
        '@skill("confused", description="wrong type for a path",\n'
        '       params={"count": {"type": "int", "path": True}})\n'
        "class Confused(Skill):\n"
        "    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:\n"
        "        return SkillResult(ok=True)\n",
        encoding="utf-8",
    )
    result = SkillContractGate().run(gate_ctx(repo))
    assert not result.passed
    assert "not 'str'" in result.details


def test_compile_passes_on_the_shipped_runtime(repo: Path) -> None:
    result = CompileGate().run(gate_ctx(repo))
    assert result.passed, result.details


def test_compile_fails_on_a_syntax_error(repo: Path) -> None:
    (repo / "src" / "ourob" / "broken_module.py").write_text("def oops(:\n", encoding="utf-8")
    result = CompileGate().run(gate_ctx(repo))
    assert not result.passed
    assert "SyntaxError" in result.details or "broken_module" in result.details


def test_import_passes_on_the_shipped_runtime(repo: Path) -> None:
    result = ImportGate().run(gate_ctx(repo))
    assert result.passed, result.details


def test_import_fails_when_a_module_is_broken(repo: Path) -> None:
    (repo / "src" / "ourob" / "kernel.py").write_text("import nonexistent_module_xyz\n", encoding="utf-8")
    result = ImportGate().run(gate_ctx(repo))
    assert not result.passed


def test_manifest_gate_passes_on_a_clean_tree(repo: Path) -> None:
    result = ManifestGate().run(gate_ctx(repo))
    assert result.passed
    assert "matches tree" in result.summary


def test_manifest_gate_fails_without_a_lock(repo: Path) -> None:
    (repo / "bootstrap.lock.json").unlink()
    result = ManifestGate().run(gate_ctx(repo))
    assert not result.passed
    assert "missing" in result.summary


def test_manifest_gate_reports_ordinary_drift_without_failing(repo: Path) -> None:
    (repo / "NEW_FILE.md").write_text("hello\n", encoding="utf-8")
    result = ManifestGate().run(gate_ctx(repo))
    assert result.passed
    assert "engineered drift" in result.summary


def test_manifest_gate_fails_on_unamended_protected_drift(repo: Path) -> None:
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# tampered\n", encoding="utf-8")
    result = ManifestGate().run(gate_ctx(repo))
    assert not result.passed
    assert "src/ourob/policies/rules.py" in result.summary


def test_manifest_gate_accepts_amended_protected_drift(repo: Path) -> None:
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# amended\n", encoding="utf-8")
    result = ManifestGate().run(gate_ctx(repo, amended_paths=["src/ourob/policies/rules.py"]))
    assert result.passed, result.summary


def test_manifest_gate_honours_a_directory_pattern_amendment(repo: Path) -> None:
    """An amendment for a protected directory covers the files under it."""
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# amended\n", encoding="utf-8")
    result = ManifestGate().run(gate_ctx(repo, amended_paths=["src/ourob/policies/"]))
    assert result.passed, result.summary
    assert "amendments considered" in result.details


def test_manifest_gate_rejects_an_amendment_for_a_different_path(repo: Path) -> None:
    (repo / "src" / "ourob" / "policies" / "rules.py").write_text("# amended\n", encoding="utf-8")
    result = ManifestGate().run(gate_ctx(repo, amended_paths=["ourob.toml"]))
    assert not result.passed
    assert "src/ourob/policies/rules.py" in result.summary


def test_lint_skips_when_ruff_is_absent(repo: Path) -> None:
    import shutil

    result = LintGate().run(gate_ctx(repo))
    if shutil.which("ruff") is None:
        assert result.skipped
        assert result.skip_reason
        assert result.passed  # a skipped advisory gate does not fail the suite
    else:  # pragma: no cover - only when ruff is installed
        assert not result.skipped


def test_tests_gate_runs_the_repos_own_suite(repo: Path) -> None:
    result = TestGate().run(gate_ctx(repo))
    assert result.passed, result.details
    assert "passed" in result.summary


def test_tests_gate_fails_on_a_failing_test(repo: Path) -> None:
    (repo / "tests" / "test_deliberate_failure.py").write_text(
        "def test_this_fails():\n    assert False\n", encoding="utf-8"
    )
    result = TestGate().run(gate_ctx(repo))
    assert not result.passed


def test_bootstrap_gate_cold_starts_the_copy(repo: Path) -> None:
    result = BootstrapGate().run(gate_ctx(repo))
    assert result.passed, result.details
    assert "TRUSTED" in result.details


# -- the suite ------------------------------------------------------------


def test_the_full_suite_passes_on_the_shipped_runtime(repo: Path) -> None:
    outcome = verify_repo(repo)
    assert outcome.passed, format_report(outcome.report, verbose=True)
    assert outcome.unknown == []
    assert len(outcome.report.gates) == len(Config.load(repo).verify.gates)


def test_the_suite_fails_when_the_tree_is_broken(repo: Path) -> None:
    (repo / "src" / "ourob" / "kernel.py").write_text("this is not python(\n", encoding="utf-8")
    outcome = verify_repo(repo, gates=["compile", "import"])
    assert not outcome.passed
    assert "compile" in [g.gate for g in outcome.report.failures]


def test_the_suite_persists_a_report(repo: Path) -> None:
    store = StateStore(repo)
    suite = VerificationSuite(repo, gates=["compile"], state=store, run_id="run-abc")
    report = suite.run(save=True)
    assert report.passed
    assert store.latest_report("run-abc") is not None
    assert (repo / ".ourob" / "verify" / "run-abc.json").is_file()


def test_blocking_gates_config_can_escalate_but_never_demote(repo: Path) -> None:
    config = Config.load(repo)
    config.verify.gates = ["lint"]
    config.verify.blocking_gates = ["lint"]
    suite = VerificationSuite(repo, config, gates=["lint"])
    report = suite.run(save=False)
    gate = report.gates[0]
    assert gate.blocking is True


def test_report_digest_changes_with_the_results() -> None:
    from ourob.state.model import GateResult

    a = VerificationReport(run_id="a")
    a.gates.append(GateResult(gate="compile", passed=True))
    b = VerificationReport(run_id="b")
    b.gates.append(GateResult(gate="compile", passed=False))
    assert a.digest != b.digest
    assert a.passed and not b.passed


def test_format_report_marks_every_state(repo: Path) -> None:
    from ourob.state.model import GateResult

    report = VerificationReport(run_id="fmt")
    report.gates = [
        GateResult(gate="compile", passed=True, summary="ok"),
        GateResult(gate="tests", passed=False, summary="boom", details="traceback here"),
        GateResult(gate="lint", passed=True, summary="skipped", skipped=True, skip_reason="no ruff"),
        GateResult(gate="style", passed=False, summary="advisory", blocking=False),
    ]
    text = format_report(report, verbose=True)
    assert "FAILED" in text
    assert "[ ok ]" in text and "[FAIL]" in text and "[SKIP]" in text and "[warn]" in text
    assert "traceback here" in text
    assert "advisory" in text
    assert report.summary_line().startswith("2/4 gates passed")


def test_the_tests_gate_asks_for_workers_when_xdist_is_present(repo: Path) -> None:
    gate = TestGate()
    ctx = gate_ctx(repo)
    args = gate._worker_args(ctx)
    if ctx.subprocess([ctx.python, "-c", "import xdist"], timeout=30)[0] == 0:
        assert args == ["-n", str(TestGate._cpu_count())], (
            "parallel=-1 must pin an explicit worker count; xdist's 'auto' "
            "resolves through psutil's *physical* core count and silently "
            "collapses to one worker on a VM"
        )
    else:
        assert args == [], "without xdist the gate must degrade to serial, not fail"


def test_the_tests_gate_can_be_pinned_to_serial(repo: Path) -> None:
    ctx = gate_ctx(repo)
    ctx.config.verify.parallel = 0
    assert TestGate()._worker_args(ctx) == []


def test_the_tests_gate_reports_the_worker_count(repo: Path) -> None:
    ctx = gate_ctx(repo)
    ctx.config.verify.parallel = 0
    result = TestGate().run(ctx)
    assert result.passed, result.details
    assert "serial workers" in result.summary
