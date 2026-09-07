"""The verification suite: the gate that stands between an edit and acceptance."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..state.model import VerificationReport
from ..state.store import StateStore
from .gates import Gate, GateContext, build_gates, unknown_gates


@dataclass
class SuiteOutcome:
    report: VerificationReport
    unknown: list[str]

    @property
    def passed(self) -> bool:
        return self.report.passed


class VerificationSuite:
    """Runs the configured gates and produces a signed-off report."""

    def __init__(
        self,
        repo: Path,
        config: Config | None = None,
        *,
        gates: list[str] | None = None,
        state: StateStore | None = None,
        run_id: str = "",
        amended_paths: list[str] | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.config = config or Config.load(self.repo)
        self.state = state or StateStore(self.repo)
        self.run_id = run_id
        self.gate_names = gates if gates is not None else list(self.config.verify.gates)
        self.amended_paths = list(amended_paths or [])

    def amendments(self) -> list[Any]:
        """Amendments that authorise protected drift during this verification.

        Two sources, merged: the paths handed in for this session (a run that
        proposed an amendment mid-flight) and whatever active amendments are
        already on file.  Both are resolved with :meth:`Amendment.covers`, so a
        directory pattern covers the files under it -- the same matcher the
        ``protected-paths`` policy uses.
        """
        from ..bootstrap.amend import Amendment, AmendmentLedger

        out: list[Any] = []
        if self.amended_paths:
            out.append(
                Amendment(
                    amendment_id=f"session:{self.run_id or 'verify'}",
                    paths=list(self.amended_paths),
                    rationale="authorised for this verification",
                )
            )
        # AmendmentLedger.all() already skips documents it cannot parse, so an
        # unreadable amendment simply fails to authorise anything -- fail closed.
        out.extend(AmendmentLedger(self.repo / ".ourob" / "amendments").active())
        return out

    def context(self) -> GateContext:
        return GateContext(
            repo=self.repo,
            config=self.config,
            timeout=self.config.verify.timeout,
            run_id=self.run_id,
            state=self.state,
            extra={"amendments": self.amendments()},
        )

    def gates(self) -> list[Gate]:
        return build_gates(self.gate_names)

    def run(self, *, save: bool = True) -> VerificationReport:
        from ..bootstrap.manifest import Manifest

        started = time.time()
        ctx = self.context()
        report = VerificationReport(run_id=self.run_id)
        for gate in self.gates():
            result = gate.run(ctx)
            # ``blocking_gates`` in the config can promote an advisory gate to blocking
            # but can never demote one; that would be self-lobotomy via configuration.
            if gate.name in self.config.verify.blocking_gates:
                result.blocking = True
            report.gates.append(result)
            if result.failed_blocking and gate.name in ("compile", "import", "policy-integrity"):
                # These three failing means the tree may not be runnable at all;
                # running the rest is noise, but we still want the report.
                continue
        report.duration_ms = int((time.time() - started) * 1000)
        try:
            report.repo_digest = Manifest.load(self.repo).digest
        except Exception:
            report.repo_digest = ""
        if save:
            self.state.save_report(report)
        return report

    def run_named(self, names: list[str], *, save: bool = False) -> VerificationReport:
        self.gate_names = names
        return self.run(save=save)


def verify_repo(
    repo: Path,
    *,
    gates: list[str] | None = None,
    run_id: str = "",
    amended_paths: list[str] | None = None,
    save: bool = True,
) -> SuiteOutcome:
    """Convenience entry point used by the CLI and by the ``run_verification`` skill."""
    config = Config.load(repo)
    names = gates if gates is not None else list(config.verify.gates)
    suite = VerificationSuite(
        repo, config, gates=names, run_id=run_id, amended_paths=amended_paths
    )
    report = suite.run(save=save)
    return SuiteOutcome(report=report, unknown=unknown_gates(names))


def format_report(report: VerificationReport, *, verbose: bool = False) -> str:
    lines = [
        f"verification {'PASSED' if report.passed else 'FAILED'} -- {report.summary_line()}"
        f"  ({report.duration_ms} ms)"
    ]
    for gate in report.gates:
        if gate.skipped:
            marker = "SKIP"
        elif gate.failed_blocking:
            marker = "FAIL"
        elif not gate.passed:
            marker = "warn"
        else:
            marker = " ok "
        flag = "" if gate.blocking else " (advisory)"
        lines.append(f"  [{marker}] {gate.gate:<18} {gate.summary}{flag}")
        if verbose and gate.details:
            lines.extend(f"         {line}" for line in gate.details.splitlines()[:20])
        if verbose and gate.skipped and gate.skip_reason:
            lines.append(f"         skipped: {gate.skip_reason}")
    return "\n".join(lines)


def report_to_dict(report: VerificationReport) -> dict[str, Any]:
    return report.to_dict()
