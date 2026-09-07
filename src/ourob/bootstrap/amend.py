"""Constitutional amendments.

Some paths are not engineering surface -- they are the parts of the runtime that
decide what engineering is allowed: the bootstrap, the policy engine, the
verification suite, and the config that names them.  A write to one of those
paths is refused outright unless an *amendment* authorises it.

An amendment is a first-class record with a rationale and a status:

``proposed``  -- opened by ``ourob amend``; authorises the write, but nothing is
                 promoted until the amendment is ratified.
``ratified``  -- the verification suite was green on the amended tree and the
                 operator accepted it.  The bootstrap lock is rewritten.
``rejected``  -- verification failed, or the operator declined.

This is the mechanism that keeps "the runtime can rewrite itself" from
collapsing into "the runtime can delete the code that says no".
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..clock import new_id, stamp
from ..errors import BootstrapError


class AmendmentStatus:
    PROPOSED = "proposed"
    RATIFIED = "ratified"
    REJECTED = "rejected"

    ALL = (PROPOSED, RATIFIED, REJECTED)


@dataclass
class Amendment:
    amendment_id: str
    paths: list[str]
    rationale: str
    status: str = AmendmentStatus.PROPOSED
    created_at: str = field(default_factory=stamp)
    ratified_at: str = ""
    proposed_by: str = "runtime"
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status in (AmendmentStatus.PROPOSED, AmendmentStatus.RATIFIED)

    def covers(self, relpath: str) -> bool:
        """True when this amendment authorises *relpath*.

        A pattern ending in ``/`` names a directory and matches everything under
        it (and the directory entry itself); anything else must match exactly.
        """
        normalised = relpath.replace("\\", "/").lstrip("./")
        for pattern in self.paths:
            pattern = pattern.replace("\\", "/").lstrip("./")
            if pattern.endswith("/"):
                if normalised.startswith(pattern) or normalised == pattern.rstrip("/"):
                    return True
            elif normalised == pattern:
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Amendment:
        return cls(
            amendment_id=data["amendment_id"],
            paths=list(data.get("paths", [])),
            rationale=data.get("rationale", ""),
            status=data.get("status", AmendmentStatus.PROPOSED),
            created_at=data.get("created_at", stamp()),
            ratified_at=data.get("ratified_at", ""),
            proposed_by=data.get("proposed_by", "runtime"),
            evidence=dict(data.get("evidence", {})),
        )

    def summary(self) -> str:
        return (
            f"{self.amendment_id} [{self.status}] "
            f"{', '.join(self.paths) or '(no paths)'} -- {self.rationale}"
        )


class AmendmentLedger:
    """Stores amendments as one JSON document each under ``.ourob/amendments``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, amendment_id: str) -> Path:
        if "/" in amendment_id or "\\" in amendment_id or amendment_id in {".", ".."}:
            raise BootstrapError(f"unsafe amendment id {amendment_id!r}")
        return self.root / f"{amendment_id}.json"

    def propose(
        self,
        paths: list[str],
        rationale: str,
        *,
        proposed_by: str = "runtime",
    ) -> Amendment:
        if not paths:
            raise BootstrapError("an amendment must name at least one path")
        if not rationale.strip():
            raise BootstrapError("an amendment requires a rationale")
        amendment = Amendment(
            amendment_id=new_id("amd"),
            paths=[p.replace("\\", "/").lstrip("./") for p in paths],
            rationale=rationale.strip(),
            proposed_by=proposed_by,
        )
        self.save(amendment)
        return amendment

    def save(self, amendment: Amendment) -> Path:
        from ..fsx import atomic_write

        path = self._path(amendment.amendment_id)
        atomic_write(path, json.dumps(amendment.to_dict(), indent=2) + "\n")
        return path

    def load(self, amendment_id: str) -> Amendment:
        path = self._path(amendment_id)
        if not path.is_file():
            raise BootstrapError(f"no amendment {amendment_id!r}")
        return Amendment.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def all(self) -> list[Amendment]:
        out = []
        for path in sorted(self.root.glob("*.json")):
            try:
                out.append(Amendment.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError):
                continue
        return out

    def active(self) -> list[Amendment]:
        return [a for a in self.all() if a.active]

    def authorising(self, relpath: str) -> Amendment | None:
        """The most recent active amendment that covers *relpath*."""
        for amendment in reversed(self.all()):
            if amendment.active and amendment.covers(relpath):
                return amendment
        return None

    def set_status(
        self, amendment_id: str, status: str, *, evidence: dict[str, Any] | None = None
    ) -> Amendment:
        if status not in AmendmentStatus.ALL:
            raise BootstrapError(f"unknown amendment status {status!r}")
        amendment = self.load(amendment_id)
        amendment.status = status
        if status == AmendmentStatus.RATIFIED:
            amendment.ratified_at = stamp()
        if evidence is not None:
            amendment.evidence = evidence
        self.save(amendment)
        return amendment
