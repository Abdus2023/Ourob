"""Verification gates.

A gate is a question with a machine-checkable answer about the current state of
the repository.  Gates run in subprocesses wherever possible so that a broken
tree cannot take the verifier down with it, and every gate has a timeout.

The suite is what stands between "the runtime edited itself" and "the edit is
accepted".  Nothing is promoted without it being green.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..state.model import GateResult
from ..state.store import StateStore


@dataclass
class GateContext:
    repo: Path
    config: Config
    python: str = sys.executable
    timeout: int = 600
    run_id: str = ""
    state: StateStore | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def subprocess(self, argv: list[str], *, timeout: int | None = None) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                argv,
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
                     "PYTHONPATH": str(self.repo / "src"), "PYTHONHASHSEED": "0"},
            )
        except subprocess.TimeoutExpired as exc:
            return 124, f"timed out after {timeout or self.timeout}s\n{exc}"
        except FileNotFoundError as exc:
            return 127, f"executable not found: {exc}"
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode, out.strip()

    def log(self, message: str) -> None:  # pragma: no cover - convenience hook
        self.extra.setdefault("log", []).append(message)


class Gate(ABC):
    """A single check.  ``name`` must match the name used in ``ourob.toml``."""

    name: str = "gate"
    title: str = "Gate"
    blocking: bool = True
    why: str = ""

    @abstractmethod
    def check(self, ctx: GateContext) -> GateResult:
        """Run the check and return a result.  Must not raise."""

    def result(
        self,
        passed: bool,
        summary: str,
        details: str = "",
        *,
        skipped: bool = False,
        skip_reason: str = "",
        started: float | None = None,
    ) -> GateResult:
        return GateResult(
            gate=self.name,
            passed=passed,
            blocking=self.blocking,
            summary=summary,
            details=details[-8000:],
            skipped=skipped,
            skip_reason=skip_reason,
            duration_ms=int((time.time() - started) * 1000) if started else 0,
        )

    def run(self, ctx: GateContext) -> GateResult:
        started = time.time()
        try:
            outcome = self.check(ctx)
        except Exception as exc:  # a gate that crashes is a failed gate
            outcome = self.result(False, f"gate raised {type(exc).__name__}: {exc}", started=started)
        if not outcome.duration_ms:
            outcome.duration_ms = int((time.time() - started) * 1000)
        return outcome


class PolicyIntegrityGate(Gate):
    """Proves the runtime has not removed its own guardrails.

    Re-loads the policy configuration from disk and checks, independently of the
    in-memory config, that the protected-path list still covers the parts of the
    system that define what is protected.  Without this gate a single edit to
    ``ourob.toml`` could legalise editing ``ourob.toml``.
    """

    name = "policy-integrity"
    title = "Guardrails are intact"
    blocking = True
    why = "The protected-path set must still cover the bootstrap, policies and verifier."

    REQUIRED_COVERAGE = (
        "bootstrap.py",
        "src/ourob/bootstrap/",
        "src/ourob/policies/",
    )

    def check(self, ctx: GateContext) -> GateResult:
        started = time.time()
        code = (
            "import json,sys;"
            f"sys.path.insert(0,{str(ctx.repo / 'src')!r});"
            "from ourob.config import Config;"
            f"c=Config.load({str(ctx.repo)!r});"
            "print(json.dumps({'protected':c.policy.protected,'source':c.source}))"
        )
        rc, out = ctx.subprocess([ctx.python, "-c", code], timeout=60)
        if rc != 0:
            return self.result(False, "could not reload policy configuration", out, started=started)
        try:
            data = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return self.result(False, "policy configuration did not parse", out, started=started)

        protected = [p.replace("\\", "/").lstrip("./") for p in data.get("protected", [])]
        missing = [
            requirement
            for requirement in self.REQUIRED_COVERAGE
            if not any(
                p.rstrip("/") == requirement.rstrip("/") or p == requirement for p in protected
            )
        ]
        if missing:
            return self.result(
                False,
                f"guardrails weakened: {', '.join(missing)} no longer protected",
                json.dumps(data, indent=2),
                started=started,
            )
        return self.result(
            True,
            f"{len(protected)} protected paths, all required coverage present",
            json.dumps(data, indent=2),
            started=started,
        )


class SkillContractGate(Gate):
    """Every registered skill must declare a valid, unique, documented contract.

    Runs in a child interpreter so that discovering skills in the tree under test
    cannot leak half-imported modules into the verifier's own process.
    """

    name = "skill-contract"
    title = "Skill contracts are well-formed"
    blocking = True
    why = "The planner can only emit safe calls if skill schemas are valid and unique."

    PROBE = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from ourob.schema import check_spec
from ourob.skills.registry import SkillRegistry

registry = SkillRegistry(sys.argv[2])
problems = list(registry.discover())
seen = {}
for entry in registry.catalogue():
    name = entry["name"]
    if name in seen:
        problems.append("duplicate skill name %r" % name)
    seen[name] = entry["module"]
    if not entry.get("description", "").strip():
        problems.append(name + ": empty description")
    problems.extend(check_spec(entry.get("params", {}), owner=name))
print(json.dumps({"skills": sorted(seen), "problems": problems}))
'''

    def check(self, ctx: GateContext) -> GateResult:
        started = time.time()
        rc, out = ctx.subprocess(
            [ctx.python, "-c", self.PROBE, str(ctx.repo / "src"), str(ctx.repo)], timeout=180
        )
        if rc != 0:
            return self.result(False, "could not introspect the skill registry", out, started=started)
        try:
            data = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return self.result(False, "skill registry probe produced no JSON", out, started=started)
        problems = data.get("problems", [])
        skills = data.get("skills", [])
        if problems:
            return self.result(
                False,
                f"{len(problems)} contract problem(s)",
                "\n".join(str(p) for p in problems),
                started=started,
            )
        if not skills:
            return self.result(False, "no skills discovered", out, started=started)
        return self.result(
            True,
            f"{len(skills)} skills with valid contracts",
            "\n".join(skills),
            started=started,
        )


class CompileGate(Gate):
    """Every byte of Python in the tree must compile."""

    name = "compile"
    title = "All Python compiles"
    blocking = True
    why = "A syntax error anywhere in the runtime makes a cold start impossible."

    def check(self, ctx: GateContext) -> GateResult:
        started = time.time()
        targets = ["src", "tests", "bootstrap.py"]
        targets = [t for t in targets if (ctx.repo / t).exists()]
        rc, out = ctx.subprocess(
            [ctx.python, "-X", "dev", "-m", "compileall", "-q", *targets], timeout=300
        )
        if rc != 0:
            return self.result(False, "compilation failed", out, started=started)
        return self.result(True, f"compiled {', '.join(targets)}", started=started)


class ImportGate(Gate):
    """The runtime must be importable in a clean interpreter, from the repo."""

    name = "import"
    title = "Runtime imports cleanly"
    blocking = True
    why = "The bootstrap imports ourob; if that fails, nothing else matters."

    PROBE = (
        "import ourob, ourob.kernel, ourob.cli, ourob.verify.suite, ourob.bootstrap.promote;"
        "import ourob.planner.llm, ourob.planner.scripted, ourob.state.store, ourob.fsx;"
        "from ourob.skills.registry import SkillRegistry;"
        "print('ourob', ourob.__version__, 'skills-ready')"
    )

    def check(self, ctx: GateContext) -> GateResult:
        started = time.time()
        rc, out = ctx.subprocess([ctx.python, "-c", self.PROBE], timeout=120)
        if rc != 0:
            return self.result(False, "import failed", out, started=started)
        return self.result(True, out.strip().splitlines()[-1] if out.strip() else "ok", started=started)


class ManifestGate(Gate):
    """Compare the tree against ``bootstrap.lock.json``.

    Drift on ordinary paths is expected while engineering and is reported, not
    failed.  Drift on a *protected* path fails unless an amendment covers it.
    Coverage is decided by :meth:`Amendment.covers` -- the same matcher the
    ``protected-paths`` policy uses -- so a directory-pattern amendment covers
    the files underneath it.
    """

    name = "manifest"
    title = "Bootstrap lock matches the tree"
    blocking = True
    why = "Protected files may only change under a ratified amendment."

    def check(self, ctx: GateContext) -> GateResult:
        started = time.time()
        from ..bootstrap.manifest import Manifest, compare

        lock = ctx.repo / "bootstrap.lock.json"
        if not lock.is_file():
            return self.result(
                False,
                "bootstrap.lock.json is missing",
                "run `python bootstrap.py --rebuild` to anchor the tree",
                started=started,
            )
        manifest = Manifest.load(ctx.repo)
        diff = compare(manifest, ctx.repo)
        amendments = list(ctx.extra.get("amendments") or [])
        violations = [
            path
            for path in diff.touched
            if ctx.config.is_protected(path)
            and not any(a.covers(path) for a in amendments)
        ]
        detail = json.dumps(diff.to_dict(), indent=2)
        if amendments:
            detail += "\namendments considered:\n" + "\n".join(
                f"  {a.amendment_id} [{a.status}] {', '.join(a.paths)}" for a in amendments
            )
        if violations:
            return self.result(
                False,
                f"unamended protected drift: {', '.join(violations)}",
                detail,
                started=started,
            )
        if diff.clean:
            return self.result(True, f"lock matches tree ({len(manifest)} files)", started=started)
        return self.result(
            True,
            f"engineered drift present ({diff.describe()}); no protected violations",
            detail,
            started=started,
        )


class LintGate(Gate):
    """Run ruff if it is installed; skip loudly if it is not."""

    name = "lint"
    title = "Linter clean"
    blocking = False
    why = "Style drift is a leading indicator of unreviewable self-modification."

    def check(self, ctx: GateContext) -> GateResult:
        started = time.time()
        binary = shutil.which("ruff")
        if binary is None:
            return self.result(
                True, "ruff not installed", skipped=True,
                skip_reason="ruff is not on PATH", started=started,
            )
        rc, out = ctx.subprocess(
            [binary, "check", "--no-cache", "src", "tests"], timeout=300
        )
        if rc != 0:
            return self.result(False, "ruff reported problems", out, started=started)
        return self.result(True, out.strip() or "ruff clean", started=started)


class TestGate(Gate):
    """Run the repository's own test suite."""

    name = "tests"
    title = "Test suite green"
    blocking = True
    why = "The tests are the runtime's specification; they must pass after self-modification."

    def check(self, ctx: GateContext) -> GateResult:
        started = time.time()
        if not (ctx.repo / "tests").is_dir():
            return self.result(
                False, "no tests/ directory",
                "a self-engineering runtime without tests has no definition of correct",
                started=started,
            )
        # No -q here: the project's own addopts may already set it, and -qq
        # suppresses the verdict line this gate reports.
        rc, out = ctx.subprocess(
            [ctx.python, "-m", "pytest", "--no-header", "-p", "no:cacheprovider", "--tb=short"],
            timeout=ctx.timeout,
        )
        lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
        tail = "\n".join(lines[-25:])
        # pytest -q ends with a verdict line ("3 passed in 0.4s"); the progress
        # bar is not a summary.  Pick the verdict if there is one.
        verdict = next(
            (line for line in reversed(lines) if any(w in line for w in ("passed", "failed", "error"))),
            lines[-1] if lines else "no output",
        )
        if rc != 0:
            return self.result(False, f"pytest exited {rc}: {verdict}", tail, started=started)
        return self.result(True, verdict, tail, started=started)


class BootstrapGate(Gate):
    """Actually perform a cold start in a subprocess and require it to succeed."""

    name = "bootstrap"
    title = "Cold start succeeds"
    blocking = True
    why = "The whole premise is that the repository can rebuild the runtime by itself."

    def check(self, ctx: GateContext) -> GateResult:
        started = time.time()
        rc, out = ctx.subprocess(
            [ctx.python, "bootstrap.py", "--prove", "--trust-drift"], timeout=180
        )
        if rc != 0:
            return self.result(False, "cold start failed", out, started=started)
        return self.result(True, "cold start verified", out, started=started)


GATE_CLASSES: dict[str, type[Gate]] = {
    cls.name: cls
    for cls in (
        PolicyIntegrityGate,
        SkillContractGate,
        CompileGate,
        ImportGate,
        ManifestGate,
        LintGate,
        TestGate,
        BootstrapGate,
    )
}


def build_gates(names: list[str]) -> list[Gate]:
    """Instantiate gates by name, preserving order and de-duplicating."""
    gates: list[Gate] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        cls = GATE_CLASSES.get(name)
        if cls is None:
            continue
        gates.append(cls())
    return gates


def unknown_gates(names: list[str]) -> list[str]:
    return [n for n in names if n not in GATE_CLASSES]
