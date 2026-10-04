"""Promotion: the only way a self-modification becomes official.

Sequence, in order, with no shortcuts:

1. diff the tree against the current bootstrap lock;
2. for protected drift, require a separately recorded operator grant pinned to
   the exact proposal, base revision, lock digest, and paths;
3. run the verification suite and require its tree digest to match the candidate;
4. on failure, reject any grant and roll back to the pre-run snapshot when one is
   available;
5. on success, require the resulting lock digest to equal the verified tree,
   commit if enabled, and record the terminal promotion event.

A change that has not been through this function is drift.  Drift does not boot
by default, does not update the lock, and is not a ratified version of the
runtime.
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import fsx
from ..clock import new_id, stamp
from ..config import Config
from ..state.model import VerificationReport
from ..state.store import StateStore
from ..verify.suite import VerificationSuite, format_report
from .amend import AmendmentLedger, AmendmentStatus
from .manifest import Manifest, ManifestDiff, compare
from .snapshot import Snapshot


@dataclass
class PromotionResult:
    accepted: bool
    report: VerificationReport | None = None
    diff: ManifestDiff = field(default_factory=ManifestDiff)
    protected: list[str] = field(default_factory=list)
    amendment_id: str = ""
    messages: list[str] = field(default_factory=list)
    rollback: dict[str, Any] = field(default_factory=dict)
    git_sha: str = ""
    prepromotion_tree_digest: str = ""
    lock_digest: str = ""
    created_at: str = field(default_factory=stamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "diff": self.diff.to_dict(),
            "protected": self.protected,
            "amendment_id": self.amendment_id,
            "messages": self.messages,
            "rollback": self.rollback,
            "git_sha": self.git_sha,
            "prepromotion_tree_digest": self.prepromotion_tree_digest,
            "lock_digest": self.lock_digest,
            "created_at": self.created_at,
            "verification": self.report.to_dict() if self.report else None,
        }

    def describe(self) -> str:
        lines = [
            f"promotion {'ACCEPTED' if self.accepted else 'REJECTED'} at {self.created_at}",
            f"  drift: {self.diff.describe()}",
        ]
        for path in self.diff.touched:
            lines.append(f"    - {path}")
        if self.protected:
            lines.append(f"  protected paths touched: {', '.join(self.protected)}")
        if self.amendment_id:
            lines.append(f"  amendment: {self.amendment_id}")
        if self.prepromotion_tree_digest:
            lines.append(f"  pre-promotion tree: {self.prepromotion_tree_digest}")
        if self.lock_digest:
            lines.append(f"  lock digest: {self.lock_digest}")
        if self.git_sha:
            lines.append(f"  commit: {self.git_sha}")
        for message in self.messages:
            lines.append(f"  - {message}")
        if self.report is not None:
            lines.append("  " + format_report(self.report).replace("\n", "\n  "))
        if self.rollback:
            lines.append(f"  rollback: {self.rollback}")
        return "\n".join(lines)


class Promotion:
    def __init__(
        self,
        repo: Path,
        config: Config | None = None,
        *,
        state: StateStore | None = None,
        use_git: bool = True,
        rollback_on_failure: bool = True,
        snapshot_run_id: str = "",
    ) -> None:
        self.repo = Path(repo).resolve()
        self.config = config or Config.load(self.repo)
        self.state = state or StateStore(self.repo)
        self.ledger = AmendmentLedger(self.repo / ".ourob" / "amendments")
        self.use_git = use_git
        self.rollback_on_failure = rollback_on_failure
        self.snapshot_run_id = snapshot_run_id

    # -- helpers ----------------------------------------------------------
    def _git(self, *argv: str, timeout: int = 60) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["git", *argv],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()

    def _commit(self, message: str) -> str:
        if not self.use_git or not (self.repo / ".git").exists():
            return ""
        rc, out = self._git("add", "-A")
        if rc != 0:
            return ""
        rc, out = self._git(
            "-c", "user.name=ourob", "-c", "user.email=ourob@localhost",
            "commit", "-q", "-m", message, "--allow-empty",
        )
        if rc != 0:
            return ""
        rc, sha = self._git("rev-parse", "--short", "HEAD")
        return sha.strip() if rc == 0 else ""

    # -- main entry -------------------------------------------------------
    def promote(
        self,
        *,
        message: str = "",
        amendment_id: str = "",
        gates: list[str] | None = None,
        save_report: bool = True,
    ) -> PromotionResult:
        result = PromotionResult(accepted=False)

        manifest = Manifest.load(self.repo)
        diff = compare(manifest, self.repo)
        result.diff = diff
        result.lock_digest = manifest.digest
        result.prepromotion_tree_digest = Manifest.build(
            self.repo, version=manifest.version
        ).digest

        protected = sorted(p for p in diff.touched if self.config.is_protected(p))
        result.protected = protected

        amendment = None
        if amendment_id:
            try:
                self.ledger.load(amendment_id)
            except Exception as exc:
                result.messages.append(f"amendment {amendment_id!r} could not be loaded: {exc}")
                return result
            amendment = self.ledger.authorising_id(amendment_id)
            if amendment is None:
                result.messages.append(
                    f"amendment {amendment_id} has no valid current operator authorization "
                    "(proposal files are not authority; check revision, digest, and history)"
                )
                return result
            if not protected:
                result.messages.append(
                    f"amendment {amendment_id} names no protected drift; refusing to consume it"
                )
                return result
            uncovered = [p for p in protected if not amendment.covers(p)]
            if uncovered:
                result.messages.append(
                    f"amendment {amendment_id} does not authorise: {', '.join(uncovered)}"
                )
                return result
            result.amendment_id = amendment_id
            result.messages.append(
                f"independent authorization {amendment_id} covers {', '.join(protected)}"
            )
        elif protected:
            result.messages.append(
                "protected paths drifted with no amendment: "
                + ", ".join(protected)
                + "; propose with `ourob amend`, then obtain an operator grant with `ourob authorize`"
            )
            self.state.record_system("promotion.rejected", result.to_dict())
            return result

        suite = VerificationSuite(
            self.repo,
            self.config,
            gates=gates,
            state=self.state,
            run_id=new_id("promote"),
        )
        report = suite.run(save=save_report)
        result.report = report

        tree_digest_matches = (
            bool(result.prepromotion_tree_digest)
            and report.tree_digest == result.prepromotion_tree_digest
        )
        if not tree_digest_matches:
            result.messages.append(
                "verification tree digest mismatch: "
                f"expected {result.prepromotion_tree_digest or '(missing)'}, "
                f"observed {report.tree_digest or '(missing)'}"
            )
        if not report.passed or not tree_digest_matches:
            if not report.passed:
                result.messages.append(
                    "verification failed: " + ", ".join(g.gate for g in report.failures)
                )
            if amendment is not None:
                self.ledger.set_status(
                    amendment.amendment_id,
                    AmendmentStatus.REJECTED,
                    evidence={"digest": report.digest, "failures": [g.gate for g in report.failures]},
                )
                result.messages.append(f"amendment {amendment.amendment_id} marked rejected")
            if self.rollback_on_failure:
                result.rollback = self._rollback()
                result.messages.append("tree rolled back to the pre-run snapshot")
            self.state.record_system("promotion.rejected", result.to_dict())
            return result

        notes = message.strip() or f"promoted at {result.created_at}"
        new_manifest = Manifest.build(self.repo, notes=notes, version=manifest.version + 1)
        if new_manifest.digest != result.prepromotion_tree_digest:
            result.messages.append(
                "tree changed after verification: "
                f"pre-promotion {result.prepromotion_tree_digest}, "
                f"lock candidate {new_manifest.digest}; promotion refused"
            )
            if amendment is not None:
                self.ledger.set_status(
                    amendment.amendment_id,
                    AmendmentStatus.REJECTED,
                    evidence={
                        "verification_digest": report.digest,
                        "prepromotion_tree_digest": result.prepromotion_tree_digest,
                        "lock_candidate_digest": new_manifest.digest,
                    },
                )
            if self.rollback_on_failure:
                result.rollback = self._rollback()
            self.state.record_system("promotion.rejected", result.to_dict())
            return result
        new_manifest.save()
        result.lock_digest = new_manifest.digest
        result.messages.append(
            f"lock rewritten ({len(new_manifest)} files) -> {new_manifest.digest[:16]}"
        )
        if amendment is not None:
            self.ledger.set_status(
                amendment.amendment_id,
                AmendmentStatus.RATIFIED,
                evidence={
                    "digest": report.digest,
                    "base_lock_digest": manifest.digest,
                    "prepromotion_tree_digest": result.prepromotion_tree_digest,
                    "lock": new_manifest.digest,
                },
            )
            result.messages.append(f"amendment {amendment.amendment_id} ratified")
        # Commit only after the grant has been consumed; the authorization is
        # pinned to the pre-promotion revision and lock digest.
        result.git_sha = self._commit(f"ourob: {notes}")
        if result.git_sha:
            result.messages.append(f"committed as {result.git_sha}")

        if self.snapshot_run_id and Snapshot.list_for(self.repo):
            with contextlib.suppress(Exception):
                Snapshot.load(self.repo, self.snapshot_run_id).discard()

        result.accepted = True
        self.state.record_system("promotion.accepted", result.to_dict())
        return result

    def _rollback(self) -> dict[str, Any]:
        """Restore the tree.  Prefers a run snapshot, falls back to git."""
        if self.snapshot_run_id:
            try:
                snapshot = Snapshot.load(self.repo, self.snapshot_run_id)
            except Exception as exc:
                return {"error": f"no snapshot for {self.snapshot_run_id}: {exc}"}
            outcome = snapshot.restore()
            outcome["source"] = f"snapshot:{self.snapshot_run_id}"
            return outcome
        if self.use_git and (self.repo / ".git").exists():
            rc, out = self._git("checkout", "--", ".")
            cleaned: list[str] = []
            rc2, listed = self._git("clean", "-nd")
            if rc2 == 0:
                for line in listed.splitlines():
                    name = line.replace("Would remove ", "").strip()
                    if name.startswith(".ourob/snapshots"):
                        continue
                    target = self.repo / name
                    if target.is_file():
                        target.unlink()
                        cleaned.append(fsx.rel(target, self.repo))
            return {"source": "git", "checkout_ok": rc == 0, "removed": cleaned, "detail": out}
        return {"error": "no snapshot and no git repository; refusing to guess a rollback"}
