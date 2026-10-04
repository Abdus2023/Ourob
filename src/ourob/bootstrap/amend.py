"""Constitutional amendment proposals and separately recorded authority.

An amendment file is a *proposal*, not permission. A protected write is
permitted only when a distinct ``amendment.authorized`` event exists in the
integrity-checked system journal and matches the exact proposal digest, base
revision, base lock digest, and path set. The standalone ``ourob authorize``
command is the explicit operator action that creates that event. Runtime
proposal skills cannot authorize their own requests.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import RLock, get_ident
from typing import Any

from ..errors import BootstrapError

_RUNTIME_EXECUTIONS = 0
_SKILL_EXECUTIONS = 0
_RUNTIME_LOCK = RLock()


@contextmanager
def skill_execution() -> Iterator[None]:
    """Mark third-party skill code so journal APIs can reject direct writes."""
    global _SKILL_EXECUTIONS
    with _RUNTIME_LOCK:
        _SKILL_EXECUTIONS += 1
    try:
        yield
    finally:
        with _RUNTIME_LOCK:
            _SKILL_EXECUTIONS -= 1


def skill_execution_active() -> bool:
    with _RUNTIME_LOCK:
        return _SKILL_EXECUTIONS > 0


@contextmanager
def runtime_execution() -> Iterator[None]:
    """Mark in-process skill dispatch so it cannot issue operator grants.

    This is an application-level capability guard, not an OS sandbox against
    arbitrary Python code that deliberately bypasses Ourob's APIs.
    """
    global _RUNTIME_EXECUTIONS
    with _RUNTIME_LOCK:
        _RUNTIME_EXECUTIONS += 1
    try:
        yield
    finally:
        with _RUNTIME_LOCK:
            _RUNTIME_EXECUTIONS -= 1


def runtime_execution_active() -> bool:
    with _RUNTIME_LOCK:
        return _RUNTIME_EXECUTIONS > 0


_OPERATOR_CONFIRMATIONS: dict[tuple[int, str, str], int] = {}


@contextmanager
def _confirmed_operator_action(amendment_id: str, confirmation: str, proposal_sha256: str) -> Iterator[None]:
    """Scope one CLI-confirmed proposal ID and digest to its calling thread."""
    if runtime_execution_active():
        raise BootstrapError("runtime execution cannot open an operator authorization action")
    if not isinstance(confirmation, str) or confirmation.strip() != amendment_id:
        raise BootstrapError("authorization confirmation did not match the proposal id")
    if not re.fullmatch(r"[0-9a-f]{64}", proposal_sha256):
        raise BootstrapError("operator confirmation requires a canonical proposal SHA-256")
    key = (get_ident(), amendment_id, proposal_sha256)
    with _RUNTIME_LOCK:
        _OPERATOR_CONFIRMATIONS[key] = _OPERATOR_CONFIRMATIONS.get(key, 0) + 1
    try:
        yield
    finally:
        with _RUNTIME_LOCK:
            remaining = _OPERATOR_CONFIRMATIONS.get(key, 1) - 1
            if remaining:
                _OPERATOR_CONFIRMATIONS[key] = remaining
            else:
                _OPERATOR_CONFIRMATIONS.pop(key, None)


def _operator_action_confirmed(amendment_id: str, proposal_sha256: str) -> bool:
    with _RUNTIME_LOCK:
        return _OPERATOR_CONFIRMATIONS.get((get_ident(), amendment_id, proposal_sha256), 0) > 0


class AmendmentStatus:
    PROPOSED = "proposed"
    # AUTHORIZED exists only on an in-memory value returned after validating a
    # separate system-journal grant; it is never a valid status in the proposal file.
    AUTHORIZED = "authorized"
    RATIFIED = "ratified"
    REJECTED = "rejected"

    ALL = (PROPOSED, RATIFIED, REJECTED)


def _normalise_pattern(value: str) -> str:
    """Canonicalise a repository-relative amendment path without traversal."""
    if not isinstance(value, str):
        raise BootstrapError("amendment paths must be strings")
    raw = value.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise BootstrapError(f"amendment path must be repository-relative: {value!r}")
    directory = raw.endswith("/")
    parts = raw.split("/")
    if any(part == ".." for part in parts):
        raise BootstrapError(f"amendment path may not contain traversal: {value!r}")
    parts = [part for part in parts if part not in ("", ".")]
    if not parts:
        raise BootstrapError(f"amendment path is empty after normalisation: {value!r}")
    return "/".join(parts) + ("/" if directory else "")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError(f"duplicate JSON key in amendment record: {key!r}")
        result[key] = value
    return result


@dataclass
class Amendment:
    amendment_id: str
    paths: list[str]
    rationale: str
    status: str = AmendmentStatus.PROPOSED
    created_at: str = ""
    ratified_at: str = ""
    proposed_by: str = "runtime"
    evidence: dict[str, Any] = field(default_factory=dict)
    base_revision: str = ""
    base_digest: str = ""

    @property
    def active(self) -> bool:
        """Whether this validated in-memory grant is usable or already ratified."""
        return self.status in (AmendmentStatus.AUTHORIZED, AmendmentStatus.RATIFIED)

    def covers(self, relpath: str) -> bool:
        """Return true only for the named file or a canonical directory prefix."""
        try:
            normalised = _normalise_pattern(relpath).rstrip("/")
        except BootstrapError:
            return False
        for pattern in self.paths:
            if pattern.endswith("/"):
                prefix = pattern.rstrip("/")
                if normalised == prefix or normalised.startswith(prefix + "/"):
                    return True
            elif normalised == pattern:
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Amendment:
        if not isinstance(data, dict):
            raise BootstrapError("amendment record is not a JSON object")
        required = {"amendment_id", "paths", "rationale"}
        if not required.issubset(data):
            raise BootstrapError("amendment record is missing required fields")
        allowed = {
            "amendment_id",
            "paths",
            "rationale",
            "status",
            "created_at",
            "ratified_at",
            "proposed_by",
            "evidence",
            "base_revision",
            "base_digest",
        }
        unknown = set(data) - allowed
        if unknown:
            raise BootstrapError(f"unknown amendment field(s): {', '.join(sorted(unknown))}")
        amendment_id = data["amendment_id"]
        rationale = data["rationale"]
        paths = data["paths"]
        if not isinstance(amendment_id, str) or not amendment_id.strip():
            raise BootstrapError("amendment id must be a non-empty string")
        if not isinstance(rationale, str) or not rationale.strip():
            raise BootstrapError("amendment rationale must be a non-empty string")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise BootstrapError("amendment paths must be a non-empty string list")
        normalised_paths = [_normalise_pattern(p) for p in paths]
        if len(set(normalised_paths)) != len(normalised_paths):
            raise BootstrapError("amendment paths contain duplicates")
        if paths != normalised_paths:
            raise BootstrapError("amendment paths are not in canonical form")
        status = data.get("status", AmendmentStatus.PROPOSED)
        if status not in AmendmentStatus.ALL:
            raise BootstrapError(f"invalid stored amendment status {status!r}")
        evidence = data.get("evidence", {})
        if not isinstance(evidence, dict):
            raise BootstrapError("amendment evidence must be a JSON object")
        created_at = data.get("created_at", "")
        ratified_at = data.get("ratified_at", "")
        proposed_by = data.get("proposed_by", "runtime")
        base_revision = data.get("base_revision", "")
        base_digest = data.get("base_digest", "")
        for label, value in (
            ("created_at", created_at),
            ("ratified_at", ratified_at),
            ("proposed_by", proposed_by),
            ("base_revision", base_revision),
            ("base_digest", base_digest),
        ):
            if not isinstance(value, str):
                raise BootstrapError(f"amendment {label} must be a string")
        return cls(
            amendment_id=amendment_id,
            paths=normalised_paths,
            rationale=rationale,
            status=status,
            created_at=created_at,
            ratified_at=ratified_at,
            proposed_by=proposed_by,
            evidence=dict(evidence),
            base_revision=base_revision,
            base_digest=base_digest,
        )

    def summary(self) -> str:
        return (
            f"{self.amendment_id} [{self.status}] {', '.join(self.paths) or '(no paths)'} -- {self.rationale}"
        )


class AmendmentLedger:
    """Proposal documents plus authority grants in the system journal."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.repo = self.root.parent.parent.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, amendment_id: str) -> Path:
        if "/" in amendment_id or "\\" in amendment_id or amendment_id in {".", ".."}:
            raise BootstrapError(f"unsafe amendment id {amendment_id!r}")
        return self.root / f"{amendment_id}.json"

    def _current_identity(self) -> tuple[str, str]:
        """The current VCS revision (or explicit unversioned marker) and lock digest."""
        revision = "unversioned"
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                revision = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        digest = ""
        try:
            from .manifest import Manifest

            digest = Manifest.load(self.repo).digest
        except Exception:
            pass
        return revision, digest

    def _validate_protected_paths(self, paths: list[str]) -> None:
        from ..config import Config

        config = Config.load(self.repo)
        uncovered = [path for path in paths if not config.is_protected(path.rstrip("/"))]
        if uncovered:
            raise BootstrapError(
                "amendments may name only currently protected paths: " + ", ".join(uncovered)
            )

    def propose(
        self,
        paths: list[str],
        rationale: str,
        *,
        proposed_by: str = "runtime",
    ) -> Amendment:
        if not paths:
            raise BootstrapError("an amendment must name at least one path")
        if not isinstance(rationale, str) or not rationale.strip():
            raise BootstrapError("an amendment requires a rationale")
        normalised = [_normalise_pattern(path) for path in paths]
        if len(set(normalised)) != len(normalised):
            raise BootstrapError("an amendment may not repeat paths")
        self._validate_protected_paths(normalised)
        if not proposed_by.strip():
            raise BootstrapError("an amendment requires a proposer identity")
        from ..clock import new_id, stamp

        revision, digest = self._current_identity()
        amendment = Amendment(
            amendment_id=new_id("amd"),
            paths=normalised,
            rationale=rationale.strip(),
            created_at=stamp(),
            proposed_by=proposed_by.strip(),
            base_revision=revision,
            base_digest=digest,
        )
        self.save(amendment)
        return amendment

    def save(self, amendment: Amendment) -> Path:
        from ..fsx import atomic_write

        if runtime_execution_active() and self._path(amendment.amendment_id).exists():
            raise BootstrapError("runtime execution cannot rewrite an existing amendment proposal")
        if amendment.status not in AmendmentStatus.ALL:
            raise BootstrapError("authorized status belongs in the system journal, not the proposal file")
        amendment.paths = [_normalise_pattern(path) for path in amendment.paths]
        self._validate_protected_paths(amendment.paths)
        path = self._path(amendment.amendment_id)
        atomic_write(path, json.dumps(amendment.to_dict(), indent=2, sort_keys=True) + "\n")
        return path

    def load(self, amendment_id: str) -> Amendment:
        path = self._path(amendment_id)
        if not path.is_file():
            raise BootstrapError(f"no amendment {amendment_id!r}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (OSError, json.JSONDecodeError) as exc:
            raise BootstrapError(f"amendment {amendment_id!r} is unreadable: {exc}") from exc
        amendment = Amendment.from_dict(data)
        if amendment.amendment_id != amendment_id:
            raise BootstrapError("amendment id does not match its filename")
        self._validate_protected_paths(amendment.paths)
        return amendment

    def all(self) -> list[Amendment]:
        out: list[Amendment] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                out.append(self.load(path.stem))
            except (BootstrapError, OSError, ValueError):
                continue
        return out

    def authorize(
        self,
        amendment_id: str,
        *,
        confirmation: str,
        authorized_by: str = "operator",
    ) -> dict[str, Any]:
        """Append an independent, single-use operator grant for one proposal."""
        if runtime_execution_active():
            raise BootstrapError(
                "runtime execution cannot authorize amendments; use a separate operator action"
            )
        if not isinstance(confirmation, str) or confirmation.strip() != amendment_id:
            raise BootstrapError("authorization confirmation did not match the proposal id")
        amendment = self.load(amendment_id)
        from ..fsx import sha256_file

        proposal_sha256 = sha256_file(self._path(amendment_id))
        if not _operator_action_confirmed(amendment_id, proposal_sha256):
            raise BootstrapError(
                "authorization requires confirmation of this exact proposal revision and digest"
            )
        if amendment.status != AmendmentStatus.PROPOSED:
            raise BootstrapError(f"amendment {amendment_id} is {amendment.status}, not proposed")
        if not authorized_by.strip():
            raise BootstrapError("authorization requires a non-empty operator identity")
        revision, digest = self._current_identity()
        if not amendment.base_revision or not amendment.base_digest:
            raise BootstrapError("proposal has no valid base revision or lock digest")
        if amendment.base_revision != revision:
            raise BootstrapError("proposal is stale: base revision does not match current HEAD")
        if amendment.base_digest != digest:
            raise BootstrapError("proposal is stale: base lock digest does not match current lock")
        from .manifest import Manifest, compare

        preexisting = [
            path for path in compare(Manifest.load(self.repo), self.repo).touched if amendment.covers(path)
        ]
        if preexisting:
            raise BootstrapError(
                "proposal cannot retroactively authorize paths already changed: " + ", ".join(preexisting)
            )

        from ..clock import new_id
        from ..state.store import StateStore

        state = StateStore(self.repo)
        corrupt = [item for item in state.check_chain() if not item["ok"]]
        if corrupt:
            raise BootstrapError(
                "cannot authorize while runtime history is corrupt: "
                + "; ".join(item["journal"] for item in corrupt)
            )
        events = state.system_events()
        consumed = {
            str(event.get("payload", {}).get("amendment_id", ""))
            for event in events
            if event.get("kind") in {"amendment.authorized", "amendment.ratified", "amendment.rejected"}
        }
        if amendment_id in consumed:
            raise BootstrapError(f"amendment {amendment_id} already has an authorization history")
        authorization_id = new_id("auth")
        payload = {
            "amendment_id": amendment_id,
            "authorization_id": authorization_id,
            "authorized_by": authorized_by.strip(),
            "proposal_sha256": proposal_sha256,
            "paths": list(amendment.paths),
            "base_revision": revision,
            "base_digest": digest,
        }
        state.record_system("amendment.authorized", payload)
        return payload

    def authorising_id(self, amendment_id: str) -> Amendment | None:
        """Return the proposal only if a unique, current, untampered grant validates."""
        try:
            amendment = self.load(amendment_id)
        except (BootstrapError, OSError, ValueError):
            return None
        if amendment.status != AmendmentStatus.PROPOSED:
            return None
        revision, digest = self._current_identity()
        if not amendment.base_revision or amendment.base_revision != revision:
            return None
        if not amendment.base_digest or amendment.base_digest != digest:
            return None

        from ..fsx import sha256_file
        from ..state.store import StateStore

        state = StateStore(self.repo)
        if any(not item["ok"] for item in state.check_chain()):
            return None
        events = state.system_events()
        terminal = [
            event
            for event in events
            if event.get("kind") in {"amendment.ratified", "amendment.rejected"}
            and event.get("payload", {}).get("amendment_id") == amendment_id
        ]
        if terminal:
            return None
        grants = [
            event
            for event in events
            if event.get("kind") == "amendment.authorized"
            and event.get("payload", {}).get("amendment_id") == amendment_id
        ]
        if len(grants) != 1:
            return None
        payload = grants[0].get("payload")
        if not isinstance(payload, dict):
            return None
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
            return None
        authorization_id = payload.get("authorization_id")
        if not isinstance(authorization_id, str) or not authorization_id:
            return None
        all_auth_ids = [
            event.get("payload", {}).get("authorization_id")
            for event in events
            if event.get("kind") == "amendment.authorized"
        ]
        if all_auth_ids.count(authorization_id) != 1:
            return None
        expected = {
            "amendment_id": amendment_id,
            "authorized_by": payload.get("authorized_by"),
            "proposal_sha256": sha256_file(self._path(amendment_id)),
            "paths": amendment.paths,
            "base_revision": amendment.base_revision,
            "base_digest": amendment.base_digest,
        }
        if not isinstance(expected["authorized_by"], str) or not expected["authorized_by"].strip():
            return None
        if any(payload.get(key) != value for key, value in expected.items()):
            return None
        return replace(amendment, status=AmendmentStatus.AUTHORIZED)

    def authorising(self, relpath: str) -> Amendment | None:
        """The most recent valid operator grant that covers ``relpath``."""
        for proposal in reversed(self.all()):
            amendment = self.authorising_id(proposal.amendment_id)
            if amendment is not None and amendment.covers(relpath):
                return amendment
        return None

    def active(self) -> list[Amendment]:
        """All currently valid operator grants; proposal JSON alone is excluded."""
        out: list[Amendment] = []
        for proposal in self.all():
            amendment = self.authorising_id(proposal.amendment_id)
            if amendment is not None:
                out.append(amendment)
        return out

    def set_status(
        self,
        amendment_id: str,
        status: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> Amendment:
        if runtime_execution_active():
            raise BootstrapError("runtime execution cannot ratify or reject amendment proposals")
        if status not in (AmendmentStatus.RATIFIED, AmendmentStatus.REJECTED):
            raise BootstrapError(f"invalid terminal amendment status {status!r}")
        amendment = self.load(amendment_id)
        if amendment.status != AmendmentStatus.PROPOSED:
            raise BootstrapError(
                f"amendment {amendment_id} is already {amendment.status}; terminal states cannot be replayed"
            )

        from ..state.store import StateStore

        state = StateStore(self.repo)
        corrupt = [item for item in state.check_chain() if not item["ok"]]
        if corrupt:
            raise BootstrapError(
                "cannot change amendment state while runtime history is corrupt: "
                + "; ".join(item["journal"] for item in corrupt)
            )
        events = state.system_events()
        prior_terminal = [
            event
            for event in events
            if event.get("kind") in {"amendment.ratified", "amendment.rejected"}
            and event.get("payload", {}).get("amendment_id") == amendment_id
        ]
        if prior_terminal:
            raise BootstrapError(f"amendment {amendment_id} already has terminal history")
        grants = [
            event
            for event in events
            if event.get("kind") == "amendment.authorized"
            and event.get("payload", {}).get("amendment_id") == amendment_id
        ]
        if len(grants) > 1:
            raise BootstrapError(f"amendment {amendment_id} has duplicate authorization history")
        grant_payload = grants[0].get("payload", {}) if grants else {}
        if status == AmendmentStatus.RATIFIED:
            from ..fsx import sha256_file

            if not grants:
                raise BootstrapError("ratification requires prior operator authorization")
            current_revision, current_digest = self._current_identity()
            grant_matches_proposal = (
                grant_payload.get("proposal_sha256") == sha256_file(self._path(amendment_id))
                and grant_payload.get("paths") == amendment.paths
                and grant_payload.get("base_revision") == amendment.base_revision
                and grant_payload.get("base_digest") == amendment.base_digest
                and current_revision == amendment.base_revision
            )
            if not grant_matches_proposal:
                raise BootstrapError("ratification does not match the original proposal grant")
            if current_digest == amendment.base_digest:
                if self.authorising_id(amendment_id) is None:
                    raise BootstrapError("only a currently valid operator grant can be ratified")
            elif not (
                isinstance(evidence, dict)
                and evidence.get("base_lock_digest") == amendment.base_digest
                and evidence.get("prepromotion_tree_digest") == evidence.get("lock") == current_digest
            ):
                raise BootstrapError("post-promotion ratification evidence does not match the resulting lock")
        elif status == AmendmentStatus.REJECTED and grants and self.authorising_id(amendment_id) is None:
            raise BootstrapError("cannot reject an amendment with an invalid or stale grant")

        amendment.status = status
        if status == AmendmentStatus.RATIFIED:
            from ..clock import stamp

            amendment.ratified_at = stamp()
        if evidence is not None:
            amendment.evidence = evidence
        self.save(amendment)
        state.record_system(
            f"amendment.{status}",
            {
                "amendment_id": amendment_id,
                "status": status,
                "evidence": amendment.evidence,
                "authorization_id": grant_payload.get("authorization_id", ""),
                "proposal_sha256": grant_payload.get("proposal_sha256", ""),
            },
        )
        return amendment
