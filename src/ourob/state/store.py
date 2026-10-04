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
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .. import fsx
from ..clock import new_id, now, stamp
from ..errors import StateError
from .model import Run, RunStatus, jsonable

GENESIS = "0" * 64


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


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
        if self.path.exists() and self.check_chain()[0]:
            for _ in self.events():
                pass  # fast-forward only an intact chain before appending

    def events(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line, object_pairs_hook=_unique_json_object)
                except ValueError as exc:  # pragma: no cover - corruption
                    raise StateError(f"{self.path}:{lineno}: malformed JSON: {exc}") from exc
                self._prev = event.get("hash", GENESIS)
                self._seq = event.get("seq", self._seq)
                yield event

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        from ..bootstrap.amend import (
            _operator_action_confirmed,
            runtime_execution_active,
            skill_execution_active,
        )

        if self.path.name == "system.jsonl" and runtime_execution_active():
            raise StateError("runtime execution cannot append system history")
        if skill_execution_active() and ".ourob" in self.path.parts:
            proposal_event = self.path.parent.name == "journal" and kind == "amendment.proposed"
            if not proposal_event:
                raise StateError("runtime skills cannot append runtime history directly")
        if kind == "amendment.authorized":
            if runtime_execution_active():
                raise StateError("runtime execution cannot append operator amendment authorizations")
            amendment_id = payload.get("amendment_id") if isinstance(payload, dict) else None
            proposal_sha256 = payload.get("proposal_sha256") if isinstance(payload, dict) else None
            if (
                not isinstance(amendment_id, str)
                or not isinstance(proposal_sha256, str)
                or not _operator_action_confirmed(amendment_id, proposal_sha256)
            ):
                raise StateError("amendment authorization requires an interactive operator confirmation")
        if self.path.name == "system.jsonl":
            # Direct callers must receive the same semantic guard as
            # StateStore.record_system; otherwise a low-level Journal.append
            # could persist a well-hashed but impossible promotion event.
            candidate = {"kind": kind, "payload": jsonable(payload)}
            detail = StateStore._check_system_semantics(self.path, candidate)
            if not detail.endswith("semantics intact"):
                raise StateError(f"refusing to append invalid system event: {detail}")
        intact, detail = self.check_chain()
        if not intact:
            raise StateError(f"refusing to append to corrupt journal {self.path}: {detail}")
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
            if self.head_path.exists():
                return False, "head anchor exists but journal is missing"
            return True, "empty journal"
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line, object_pairs_hook=_unique_json_object)
                    if not isinstance(event, dict):
                        return False, f"line {lineno}: event is not a JSON object"
                    recorded = event.pop("hash", None)
                    if type(event.get("seq")) is not int or event["seq"] != count + 1:
                        return False, f"line {lineno}: sequence is not contiguous"
                    if not isinstance(event.get("kind"), str) or not event["kind"]:
                        return False, f"line {lineno}: event kind is missing"
                    if not isinstance(event.get("payload"), dict):
                        return False, f"line {lineno}: payload is not an object"
                    if event.get("prev") != prev:
                        return False, f"line {lineno}: prev does not match previous hash"
                    digest = fsx.sha256_text(json.dumps(event, sort_keys=True, separators=(",", ":")))
                    if digest != recorded:
                        return False, f"line {lineno}: content hash mismatch"
                    prev = recorded
                    count += 1
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return False, f"journal is unreadable: {type(exc).__name__}: {exc}"
        if count and not self.head_path.exists():
            return False, "head anchor is missing"
        if self.head_path.exists():
            try:
                head = json.loads(
                    self.head_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_unique_json_object,
                )
            except (OSError, ValueError):
                return False, "head anchor is unreadable"
            if not isinstance(head, dict):
                return False, "head anchor is not a JSON object"
            if set(head) != {"seq", "hash"}:
                return False, "head anchor has missing or unexpected fields"
            head_seq = head["seq"]
            if type(head_seq) is not int:
                return False, "head anchor sequence is invalid"
            if head_seq != count:
                return False, (
                    f"truncated: the log holds {count} events but the head anchor records {head.get('seq')}"
                )
            if head.get("hash") != prev:
                return False, "head anchor does not match the last event"
        return True, f"{count} events, chain intact"


class StateStore:
    """Owns ``.ourob/`` and everything durable in it."""

    def __init__(self, repo: Path, *, state_dir: str | Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        state_path = self.repo / (state_dir or ".ourob")
        if state_dir is None and state_path.is_symlink():
            raise StateError("runtime state root .ourob must not be a symlink")
        self.root = state_path.resolve()
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
        """Record a non-run event, refusing to extend corrupt or impossible history."""
        from ..bootstrap.amend import runtime_execution_active, skill_execution_active

        if runtime_execution_active() or skill_execution_active():
            raise StateError("runtime skills cannot append system history")
        if kind == "amendment.authorized":
            from ..bootstrap.amend import _operator_action_confirmed, runtime_execution_active

            if runtime_execution_active():
                raise StateError("runtime execution cannot append operator amendment authorizations")
            amendment_id = payload.get("amendment_id") if isinstance(payload, dict) else None
            proposal_sha256 = payload.get("proposal_sha256") if isinstance(payload, dict) else None
            if (
                not isinstance(amendment_id, str)
                or not isinstance(proposal_sha256, str)
                or not _operator_action_confirmed(amendment_id, proposal_sha256)
            ):
                raise StateError("amendment authorization requires an interactive operator confirmation")
        corrupt = [item for item in self.check_chain() if not item["ok"]]
        if corrupt:
            raise StateError(
                "refusing to append to invalid runtime history: "
                + "; ".join(item["journal"] for item in corrupt)
            )
        path = self.root / "system.jsonl"
        candidate = {"kind": kind, "payload": jsonable(payload)}
        detail = self._check_system_semantics(path, candidate)
        if not detail.endswith("semantics intact"):
            raise StateError(f"refusing to append invalid system event: {detail}")
        return Journal(path).append(kind, payload)

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
                "touched": run.touched,
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
                "touched": run.touched,
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
                        touched=payload.get("touched", []),
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
                    step.status = StepStatus.ALLOWED if payload.get("allowed") else StepStatus.DENIED
            elif kind == "skill.result":
                step = steps.get(int(payload["index"]))
                if step is not None:
                    step.result = SkillResult.from_dict(payload.get("result", {}))
                    # Artifacts are re-derived from the log so that a replayed run
                    # reports the same touched set as the original one did.
                    run.record_artifacts(step.result.artifacts)
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
        from ..bootstrap.amend import skill_execution_active

        if skill_execution_active():
            raise StateError(
                "runtime skills cannot persist verification reports; use the verifier's final run report"
            )
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
    @staticmethod
    def _is_digest(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    @classmethod
    def _check_system_semantics(cls, path: Path, extra_event: dict[str, Any] | None = None) -> str:
        """Validate system-event schemas and one-way authorization transitions.

        Hash chains detect edits to stored bytes; these checks additionally reject
        well-hashed but impossible or contradictory state transitions. This is
        application-level consistency, not proof of a human identity or a MAC.
        """
        try:
            events = list(Journal(path).events())
            if extra_event is not None:
                events.append(extra_event)
        except (OSError, StateError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return f"system journal cannot be replayed: {type(exc).__name__}: {exc}"

        grants: dict[str, dict[str, Any]] = {}
        authorization_ids: set[str] = set()
        terminals: dict[str, str] = {}
        for index, event in enumerate(events, start=1):
            kind = event.get("kind")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                return f"system event {index}: payload is not an object"

            if kind == "amendment.authorized":
                required = {
                    "amendment_id",
                    "authorization_id",
                    "authorized_by",
                    "proposal_sha256",
                    "paths",
                    "base_revision",
                    "base_digest",
                }
                if set(payload) != required:
                    return f"system event {index}: malformed amendment authorization fields"
                amendment_id = payload.get("amendment_id")
                authorization_id = payload.get("authorization_id")
                actor = payload.get("authorized_by")
                paths = payload.get("paths")
                if not isinstance(amendment_id, str) or not amendment_id.strip():
                    return f"system event {index}: amendment id is invalid"
                if not isinstance(authorization_id, str) or not authorization_id.strip():
                    return f"system event {index}: authorization id is invalid"
                if amendment_id in grants or amendment_id in terminals:
                    return f"system event {index}: amendment authorization is duplicated or replayed"
                if authorization_id in authorization_ids:
                    return f"system event {index}: authorization id is duplicated"
                if (
                    not isinstance(actor, str)
                    or not actor.strip()
                    or actor.strip().lower() in {"runtime", "planner", "skill"}
                ):
                    return f"system event {index}: authorization actor is not an operator identity"
                if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
                    return f"system event {index}: authorization paths are invalid"
                canonical: list[str] = []
                for pattern in paths:
                    raw = pattern.replace("\\", "/").strip()
                    if (
                        not raw
                        or raw.startswith("/")
                        or re.match(r"^[A-Za-z]:", raw)
                        or any(part == ".." for part in raw.split("/"))
                    ):
                        return f"system event {index}: authorization path is not confined"
                    directory = raw.endswith("/")
                    parts = [part for part in raw.split("/") if part not in ("", ".")]
                    if not parts:
                        return f"system event {index}: authorization path is empty"
                    canonical.append("/".join(parts) + ("/" if directory else ""))
                if canonical != paths or len(set(canonical)) != len(canonical):
                    return f"system event {index}: authorization paths are noncanonical or duplicated"
                if not isinstance(payload.get("base_revision"), str) or not payload["base_revision"].strip():
                    return f"system event {index}: base revision is invalid"
                if not cls._is_digest(payload.get("proposal_sha256")):
                    return f"system event {index}: proposal digest is invalid"
                if not cls._is_digest(payload.get("base_digest")):
                    return f"system event {index}: base lock digest is invalid"
                grants[amendment_id] = payload
                authorization_ids.add(authorization_id)
                continue

            if kind in {"amendment.ratified", "amendment.rejected"}:
                required = {"amendment_id", "status", "evidence", "authorization_id", "proposal_sha256"}
                if set(payload) != required:
                    return f"system event {index}: malformed amendment terminal fields"
                amendment_id = payload.get("amendment_id")
                expected_status = kind.removeprefix("amendment.")
                if payload.get("status") != expected_status:
                    return f"system event {index}: terminal status disagrees with event kind"
                if not isinstance(amendment_id, str) or not amendment_id.strip():
                    return f"system event {index}: terminal amendment id is invalid"
                if not isinstance(payload.get("evidence"), dict):
                    return f"system event {index}: terminal evidence is invalid"
                if amendment_id in terminals:
                    return f"system event {index}: amendment has more than one terminal event"
                grant = grants.get(amendment_id)
                authorization_id = payload.get("authorization_id")
                proposal_digest = payload.get("proposal_sha256")
                if expected_status == "ratified" and grant is None:
                    return f"system event {index}: ratification has no prior authorization"
                if grant is None:
                    if authorization_id != "" or proposal_digest != "":
                        return f"system event {index}: terminal event cites a nonexistent grant"
                elif authorization_id != grant.get("authorization_id") or proposal_digest != grant.get(
                    "proposal_sha256"
                ):
                    return f"system event {index}: terminal event does not match its grant"
                terminals[amendment_id] = expected_status
                continue

            if kind == "promotion.accepted":
                if payload.get("accepted") is not True:
                    return f"system event {index}: accepted promotion has false acceptance state"
                tree_digest = payload.get("prepromotion_tree_digest")
                lock_digest = payload.get("lock_digest")
                verification = payload.get("verification")
                if not cls._is_digest(tree_digest) or lock_digest != tree_digest:
                    return f"system event {index}: accepted promotion lock/tree digest mismatch"
                if (
                    not isinstance(verification, dict)
                    or verification.get("passed") is not True
                    or verification.get("tree_digest") != tree_digest
                ):
                    return f"system event {index}: accepted promotion lacks matching verification evidence"
                amendment_id = payload.get("amendment_id")
                if amendment_id and terminals.get(amendment_id) != "ratified":
                    return f"system event {index}: accepted promotion amendment is not ratified"
                continue

            if kind == "promotion.rejected":
                if payload.get("accepted") is not False:
                    return f"system event {index}: rejected promotion has inconsistent acceptance state"
                continue

            return f"system event {index}: unknown system event kind {kind!r}"
        return f"{len(events)} system events, semantics intact"

    def check_chain(self) -> list[dict[str, Any]]:
        """Check every run, index, and system-history log plus its head anchor."""
        results: list[dict[str, Any]] = []
        paths = sorted(self.journals.glob("*.jsonl")) + [
            self._index.path,
            self.root / "system.jsonl",
        ]
        system_path = (self.root / "system.jsonl").resolve(strict=False)
        for path in paths:
            ok, detail = Journal(path).check_chain()
            if ok and path.resolve(strict=False) == system_path:
                detail = self._check_system_semantics(path)
                ok = detail.endswith("semantics intact")
            results.append({"journal": fsx.rel(path, self.repo), "ok": ok, "detail": detail})
        # A detached anchor is evidence of a deleted log even when the .jsonl
        # itself no longer exists and therefore was not found by the glob.
        known = {path.resolve(strict=False) for path in paths}
        for head in sorted(self.journals.glob("*.jsonl.head")):
            journal = head.with_name(head.name.removesuffix(".head"))
            if journal.resolve(strict=False) not in known:
                results.append(
                    {
                        "journal": fsx.rel(journal, self.repo),
                        "ok": False,
                        "detail": "head anchor exists but journal is missing",
                    }
                )
        return results
