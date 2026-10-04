"""Self-inspection and self-verification skills.

These are how the runtime reasons about itself from inside a run: what can I do
(``list_skills``), what do I consist of (``read_manifest``), am I still healthy
(``run_verification``), and what do I need permission for
(``propose_amendment``).
"""

from __future__ import annotations

import json
from typing import Any

from ... import fsx
from ...errors import SkillError
from ...state.model import SkillResult
from ..base import Skill, SkillContext, skill


@skill(
    "run_verification",
    title="Run the verification suite",
    description=(
        "Run the repository's verification gates and return the report. Use this "
        "after any change to the runtime before considering the work done."
    ),
    params={
        "gates": {
            "type": "list",
            "default": [],
            "desc": "subset of gate names to run; empty means the configured suite",
        },
        "save": {"type": "bool", "default": False},
    },
)
class RunVerification(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        from ...verify.suite import VerificationSuite, format_report

        names = [str(g) for g in (kwargs.get("gates") or [])] or None
        suite = VerificationSuite(
            ctx.repo,
            ctx.services.get("config"),
            gates=names,
            state=ctx.store,
            run_id=ctx.run_id,
        )
        if bool(kwargs.get("save", False)):
            raise SkillError(
                "a runtime skill cannot write verification reports into protected state; "
                "use `ourob verify` or rely on the kernel's final verification report"
            )
        report = suite.run(save=False)
        text = format_report(report, verbose=True)
        return SkillResult(
            ok=report.passed,
            output=text,
            data={
                "passed": report.passed,
                "digest": report.digest,
                "summary": report.summary_line(),
                "failures": [g.gate for g in report.failures],
                "gates": report.to_dict()["gates"],
            },
            error=None if report.passed else "verification failed",
        )


@skill(
    "run_tests",
    title="Run the test suite",
    description="Shortcut for running pytest over the repository's own tests.",
    params={"timeout": {"type": "int", "default": 600, "min": 10, "max": 3600}},
)
class RunTests(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        from ...verify.gates import TestGate
        from ...verify.suite import VerificationSuite

        suite = VerificationSuite(ctx.repo, ctx.services.get("config"), state=ctx.store, run_id=ctx.run_id)
        gate_ctx = suite.context()
        gate_ctx.timeout = int(kwargs.get("timeout", 600))
        result = TestGate().run(gate_ctx)
        return SkillResult(
            ok=result.passed,
            output=f"{result.summary}\n{result.details}",
            data={"gate": result.to_dict()},
            error=None if result.passed else result.summary,
        )


@skill(
    "list_skills",
    title="List available skills",
    description="Return the catalogue of skills the runtime currently has.",
    params={"pattern": {"type": "str", "default": "", "desc": "substring filter on the name"}},
)
class ListSkills(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        registry = ctx.service("registry")
        pattern = kwargs.get("pattern") or ""
        catalogue = [c for c in registry.catalogue() if pattern in c["name"]]
        lines = [
            f"{c['name']:<18} {'mutating' if c['mutating'] else 'read-only'}  {c['description']}"
            for c in catalogue
        ]
        problems = registry.problems()
        discovery = registry.discovery_status()
        if discovery["restart_required"]:
            pending = [path for paths in discovery["pending"].values() for path in paths]
            lines.append(
                "\ndiscovery snapshot is stale; verify/promote and start a fresh runtime "
                f"before using pending files: {', '.join(pending)}"
            )
        if problems:
            lines.append("\ndiscovery problems:")
            lines.extend(f"  - {p}" for p in problems)
        return SkillResult(
            ok=True,
            output="\n".join(lines) or "(no skills)",
            data={
                "skills": catalogue,
                "count": len(catalogue),
                "problems": problems,
                "discovery": discovery,
            },
        )


@skill(
    "read_manifest",
    title="Read the bootstrap manifest",
    description=(
        "Report the self-describing manifest and how the working tree currently "
        "differs from the last ratified state."
    ),
    params={
        "path": {
            "type": "str",
            "default": "",
            "path": True,
            "desc": "restrict the diff to one path",
        }
    },
)
class ReadManifest(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        from ...bootstrap.manifest import Manifest, compare

        lock = ctx.repo / "bootstrap.lock.json"
        if not lock.is_file():
            raise SkillError("bootstrap.lock.json does not exist; the tree is unanchored")
        manifest = Manifest.load(ctx.repo)
        diff = compare(manifest, ctx.repo)
        focus = kwargs.get("path") or ""
        data = {
            "digest": manifest.digest,
            "files": len(manifest),
            "protected": manifest.protected_paths,
            "drift": diff.to_dict(),
            "created_at": manifest.created_at,
            "version": manifest.version,
        }
        if focus:
            normalised = focus.replace("\\", "/").lstrip("./")
            record = manifest.files.get(normalised)
            data["focus"] = {
                "path": focus,
                "record": record.to_dict() if record is not None else None,
                "changed": normalised in diff.touched,
                "protected": ctx.services.get("config").is_protected(focus)
                if ctx.services.get("config")
                else None,
            }
        payload = json.dumps(data, indent=2, sort_keys=True)
        return SkillResult(
            ok=True,
            output=(
                f"lock {manifest.digest[:16]} | {len(manifest)} files | drift: {diff.describe()}\n{payload}"
            ),
            data=data,
        )


@skill(
    "propose_amendment",
    title="Propose a constitutional amendment",
    description=(
        "Create a proposal describing a possible protected-path change. A proposal "
        "does not authorize writes; only a separate operator grant recorded by "
        "the standalone authorize command can do that."
    ),
    params={
        "paths": {"type": "list", "required": True, "desc": "protected paths to authorise"},
        "rationale": {"type": "str", "required": True, "desc": "why the change is necessary"},
    },
    mutating=True,
)
class ProposeAmendment(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        from ...bootstrap.amend import AmendmentLedger

        ledger = ctx.services.get("ledger") or AmendmentLedger(ctx.repo / ".ourob" / "amendments")
        paths = [str(p) for p in kwargs["paths"]]
        amendment = ledger.propose(paths, str(kwargs["rationale"]), proposed_by="runtime")
        if ctx.store is not None:
            ctx.store.record(ctx.run_id, "amendment.proposed", amendment.to_dict())
        return SkillResult(
            ok=True,
            output=amendment.summary(),
            data=amendment.to_dict(),
            artifacts=[fsx.rel(ledger._path(amendment.amendment_id), ctx.repo)],
        )


@skill(
    "finish",
    title="Finish the run",
    description=(
        "Declare the run complete with an outcome and a summary. Calling this ends the planning loop."
    ),
    params={
        "summary": {"type": "str", "required": True},
        "success": {"type": "bool", "default": True},
    },
)
class Finish(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        summary = str(kwargs["summary"])
        success = bool(kwargs.get("success", True))
        return SkillResult(
            ok=True,
            output=summary,
            data={"summary": summary, "success": success, "terminal": True},
        )
