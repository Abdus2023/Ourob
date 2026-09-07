"""Durable state: an append-only, hash-chained journal.

The journal is the runtime's memory of what it did to itself.  Each line
records the SHA-256 of the line before it, so any edit, deletion or truncation
of history is detectable with :meth:`StateStore.check_chain`.  A system that
rewrites its own source needs exactly this property: the record of the change
must be harder to tamper with than the change itself.

Layout inside ``.ourob/``::

    journal/<run_id>.jsonl      one event per line, hash-chained
    index.jsonl                 one summary line per run, hash-chained
    verify/<run_id>.json        verification reports
    amendments/<id>.json        proposed/ratified constitutional amendments
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .. import fsx
from ..clock import new_id, now, stamp
from ..errors import StateError
from .model import Run, RunStatus, jsonable

GENESIS = "0" * 64


def _chain(prev: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body["prev"] = prev
    body["hash"] = fsx.sha256_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
    return body


class Journal:
    """One run's event log.

    Each line records the hash of the line before it, so an edited or reordered
    line breaks the chain.  A pure chain cannot notice *truncation* -- dropping
    the tail still leaves a valid prefix -- so the journal also keeps a ``.head``
    anchor naming the last sequence number and hash.  Appending rewrites the
    anchor atomically; a log shorter than its anchor has been truncated.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_name(self.path.name + ".head")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = GENESIS
        self._seq = 0
        for _ in self.events():
            pass  # fast-forward chain state to the end of an existing log

    def events(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:  # pragma: no cover - corruption
                    raise StateError(f"{self.path}:{lineno}: malformed JSON: {exc}") from exc
                self._prev = event.get("hash", GENESIS)
                self._seq = event.get("seq", self._seq)
                yield event

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        event = _chain(
            self._prev,
            {
                "seq": self._seq,
                "kind": kind,
                "ts": now(),
                "stamp": stamp(),
                "payload": jsonable(payload),
            },
        )
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._prev = event["hash"]
        self._write_head()
        return event

    def _write_head(self) -> None:
        from ..fsx import atomic_write

        atomic_write(
            self.head_path,
            json.dumps({"seq": self._seq, "hash": self._prev}, sort_keys=True) + "\n",
        )

    def check_chain(self) -> tuple[bool, str]:
        prev = GENESIS
        count = 0
        if not self.path.exists():
            return True, "empty journal"
        with self.path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                recorded = event.pop("hash", None)
                if event.get("prev") != prev:
                    return False, f"line {lineno}: prev does not match previous hash"
                if fsx.sha256_text(json.dumps(event, sort_keys=True, separators=(",", ":"))) != recorded:
                    return False, f"line {lineno}: content hash mismatch"
                prev = recorded
                count += 1
        if self.head_path.exists():
            try:
                head = json.loads(self.head_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return False, "head anchor is unreadable"
            if int(head.get("seq", -1)) != count:
                return False, (
                    f"truncated: the log holds {count} events but the head anchor "
                    f"records {head.get('seq')}"
                )
            if head.get("hash") != prev:
                return False, "head anchor does not match the last event"
        return True, f"{count} events, chain intact"


class StateStore:
    """Owns ``.ourob/`` and everything durable in it."""

    def __init__(self, repo: Path, *, state_dir: str | Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.root = (self.repo / (state_dir or ".ourob")).resolve()
        self.journals = self.root / "journal"
        self.verify_dir = self.root / "verify"
        self.amendments = self.root / "amendments"
        for directory in (self.journals, self.verify_dir, self.amendments):
            directory.mkdir(parents=True, exist_ok=True)
        self._index = Journal(self.root / "index.jsonl")

    # -- runs -------------------------------------------------------------
    def open_run(self, goal: str, *, max_steps: int = 32, planner: str = "unknown") -> Run:
        run = Run.new(goal, max_steps=max_steps, planner=planner)
        self._index.append("run.opened", {"run_id": run.run_id, "goal": goal, "planner": planner})
        self.journal(run.run_id).append(
            "run.started",
            {"run_id": run.run_id, "goal": goal, "max_steps": max_steps, "planner": planner},
        )
        return run

    def journal(self, run_id: str) -> Journal:
        if "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise StateError(f"unsafe run id {run_id!r}")
        return Journal(self.journals / f"{run_id}.jsonl")

    def record(self, run_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.journal(run_id).append(kind, payload)

    def record_system(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Record an event that is not part of any run (promotions, amendments)."""
        return Journal(self.root / "system.jsonl").append(kind, payload)

    def system_events(self) -> list[dict[str, Any]]:
        return list(Journal(self.root / "system.jsonl").events())

    def close_run(self, run: Run) -> None:
        run.ended_at = now()
        self.journal(run.run_id).append(
            "run.finished",
            {
                "status": run.status,
                "outcome": run.outcome,
                "steps": run.step_count,
                "ended_at": run.ended_at,
            },
        )
        self._index.append(
            "run.closed",
            {
                "run_id": run.run_id,
                "goal": run.goal,
                "status": run.status,
                "steps": run.step_count,
                "outcome": run.outcome,
            },
        )

    def list_runs(self) -> list[dict[str, Any]]:
        runs: dict[str, dict[str, Any]] = {}
        if self._index.path.exists():
            for event in self._index.events():
                payload = event.get("payload", {})
                run_id = payload.get("run_id")
                if not run_id:
                    continue
                if event["kind"] == "run.opened":
                    runs[run_id] = {
                        "run_id": run_id,
                        "goal": payload.get("goal", ""),
                        "planner": payload.get("planner", ""),
                        "opened": event.get("stamp"),
                        "status": RunStatus.RUNNING.value,
                    }
                elif event["kind"] == "run.closed":
                    entry = runs.setdefault(run_id, {"run_id": run_id})
                    entry.update(
                        status=payload.get("status"),
                        steps=payload.get("steps"),
                        outcome=payload.get("outcome"),
                        closed=event.get("stamp"),
                    )
        return [runs[k] for k in sorted(runs)]

    def replay(self, run_id: str) -> Run:
        """Rebuild a :class:`Run` from its journal."""
        from .model import Invocation, PolicyDecision, SkillResult, Step, StepStatus

        run: Run | None = None
        steps: dict[int, Step] = {}
        for event in self.journal(run_id).events():
            kind = event["kind"]
            payload = event.get("payload", {})
            if kind == "run.started":
                run = Run(
                    run_id=payload["run_id"],
                    goal=payload.get("goal", ""),
                    planner=payload.get("planner", "unknown"),
                    max_steps=int(payload.get("max_steps", 32)),
                    started_at=event.get("ts", now()),
                )
            elif kind == "step.planned":
                step = Step(
                    index=int(payload["index"]),
                    invocation=Invocation.from_dict(payload["invocation"]),
                    status=StepStatus.PLANNED,
                )
                steps[step.index] = step
            elif kind == "policy.decision":
                step = steps.get(int(payload["index"]))
                if step is not None:
                    for decision in payload.get("decisions", []):
                        step.decisions.append(PolicyDecision(**decision))
                    step.status = (
                        StepStatus.ALLOWED if payload.get("allowed") else StepStatus.DENIED
                    )
            elif kind == "skill.result":
                step = steps.get(int(payload["index"]))
                if step is not None:
                    step.result = SkillResult.from_dict(payload.get("result", {}))
                    if payload.get("denied"):
                        step.status = StepStatus.DENIED
                    elif step.status is not StepStatus.DENIED:
                        step.status = StepStatus.OK if step.result.ok else StepStatus.ERROR
                    step.ended_at = event.get("ts")
            elif kind == "run.finished" and run is not None:
                run.status = RunStatus(payload.get("status", RunStatus.RUNNING.value))
                run.outcome = payload.get("outcome", "")
                run.ended_at = event.get("ts")
        if run is None:
            raise StateError(f"no run {run_id!r} in the journal")
        run.steps = [steps[i] for i in sorted(steps)]
        report = self.latest_report(run_id)
        if report is not None:
            run.verification = report
        return run

    # -- verification reports ---------------------------------------------
    def save_report(self, report: Any) -> Path:
        path = self.verify_dir / f"{report.run_id or new_id('verify')}.json"
        fsx.atomic_write(path, json.dumps(report.to_dict(), indent=2, sort_keys=True))
        self._index.append(
            "verification.report",
            {"run_id": report.run_id, "passed": report.passed, "digest": report.digest},
        )
        return path

    def latest_report(self, run_id: str) -> Any | None:
        from .model import VerificationReport

        path = self.verify_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return VerificationReport.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def reports(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.verify_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append(
                {
                    "run_id": data.get("run_id"),
                    "passed": data.get("passed"),
                    "created": data.get("created_stamp"),
                    "digest": data.get("digest", "")[:12],
                    "path": fsx.rel(path, self.repo),
                }
            )
        return out

    # -- integrity --------------------------------------------------------
    def check_chain(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.journals.glob("*.jsonl")) + [self._index.path]:
            ok, detail = Journal(path).check_chain()
            results.append({"journal": fsx.rel(path, self.repo), "ok": ok, "detail": detail})
        return results
