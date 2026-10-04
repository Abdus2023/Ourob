"""The self-describing manifest.

``bootstrap.lock.json`` is the runtime's statement about its own source: every
file it considers part of itself, with a content hash.  Three things consume it:

* :mod:`ourob.bootstrap.coldstart` refuses to boot from a tree that does not
  match the last ratified state (unless told to proceed);
* the ``manifest`` verification gate reports drift on protected paths;
* :mod:`ourob.bootstrap.promote` rewrites it when a change set is ratified, so
  the lock always describes the most recent *verified* version of the runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import fsx
from ..clock import stamp
from ..errors import BootstrapError

LOCK_NAME = "bootstrap.lock.json"


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha256: str
    size: int
    protected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "protected": self.protected,
        }


@dataclass
class Manifest:
    repo: Path
    files: dict[str, FileRecord] = field(default_factory=dict)
    created_at: str = ""
    version: int = 1
    notes: str = ""

    # -- construction -----------------------------------------------------
    @classmethod
    def build(
        cls,
        repo: Path,
        *,
        protected_prefixes: list[str] | None = None,
        notes: str = "",
        version: int = 1,
    ) -> Manifest:
        from ..config import Config  # local import: config depends on nothing here

        repo = Path(repo).resolve()
        config = Config.load(repo)
        prefixes = protected_prefixes if protected_prefixes is not None else config.policy.protected

        def _protected(relpath: str) -> bool:
            normalised = relpath.lstrip("./")
            return any(
                normalised.startswith(p.rstrip("/") + "/") or normalised == p.rstrip("/")
                for p in prefixes
            )

        files: dict[str, FileRecord] = {}
        for path in fsx.walk_repo(repo):
            relpath = fsx.rel(path, repo)
            if relpath == LOCK_NAME:
                continue
            files[relpath] = FileRecord(
                path=relpath,
                sha256=fsx.sha256_file(path),
                size=path.stat().st_size,
                protected=_protected(relpath),
            )
        return cls(
            repo=repo,
            files=files,
            created_at=stamp(),
            notes=notes,
            version=version,
        )

    @classmethod
    def load(cls, repo: Path) -> Manifest:
        repo = Path(repo).resolve()
        path = repo / LOCK_NAME
        if not path.is_file():
            raise BootstrapError(f"no {LOCK_NAME}; run `python bootstrap.py --rebuild`")
        data = json.loads(path.read_text(encoding="utf-8"))
        manifest = cls(
            repo=repo,
            created_at=data.get("created_at", ""),
            version=int(data.get("version", 1)),
            notes=data.get("notes", ""),
        )
        for entry in data.get("files", []):
            record = FileRecord(
                path=entry["path"],
                sha256=entry["sha256"],
                size=int(entry.get("size", 0)),
                protected=bool(entry.get("protected", False)),
            )
            manifest.files[record.path] = record
        return manifest

    def save(self) -> Path:
        payload = {
            "format": "ourob.bootstrap.lock/v1",
            "version": self.version,
            "created_at": self.created_at,
            "notes": self.notes,
            "digest": self.digest,
            "files": [self.files[k].to_dict() for k in sorted(self.files)],
        }
        path = self.repo / LOCK_NAME
        fsx.atomic_write(path, json.dumps(payload, indent=2) + "\n")
        return path

    # -- introspection ----------------------------------------------------
    @property
    def digest(self) -> str:
        return fsx.sha256_text(
            "\n".join(f"{k} {self.files[k].sha256}" for k in sorted(self.files))
        )

    @property
    def protected_paths(self) -> list[str]:
        return sorted(p for p, r in self.files.items() if r.protected)

    def paths(self) -> list[str]:
        return sorted(self.files)

    def __len__(self) -> int:
        return len(self.files)


@dataclass
class ManifestDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.added or self.removed or self.changed or self.missing)

    @property
    def touched(self) -> list[str]:
        return sorted(set(self.added) | set(self.removed) | set(self.changed))

    def protected_changes(self, manifest: Manifest) -> list[str]:
        """Drift that touches a protected path -- requires separate authority."""
        flagged: set[str] = set()
        for relpath in self.added + self.changed + self.removed:
            record = manifest.files.get(relpath)
            if record is not None and record.protected:
                flagged.add(relpath)
                continue
            # a new file inside a protected directory is protected too
            if any(relpath.startswith(p) for p in manifest.protected_paths if p.endswith("/")):
                flagged.add(relpath)
        return sorted(flagged)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "missing": self.missing,
            "clean": self.clean,
        }

    def describe(self) -> str:
        if self.clean:
            return "no drift"
        parts = []
        for label, items in (
            ("added", self.added),
            ("changed", self.changed),
            ("removed", self.removed),
            ("missing", self.missing),
        ):
            if items:
                parts.append(f"{label}={len(items)}")
        return ", ".join(parts)


def compare(manifest: Manifest, repo: Path | None = None) -> ManifestDiff:
    """Diff a manifest against the working tree."""
    repo = Path(repo or manifest.repo).resolve()
    diff = ManifestDiff()
    seen: set[str] = set()
    for relpath, record in sorted(manifest.files.items()):
        seen.add(relpath)
        target = repo / relpath
        if not target.is_file():
            diff.missing.append(relpath)
            continue
        if fsx.sha256_file(target) != record.sha256:
            diff.changed.append(relpath)
    for path in fsx.walk_repo(repo):
        relpath = fsx.rel(path, repo)
        if relpath == LOCK_NAME or relpath in seen:
            continue
        diff.added.append(relpath)
    return diff
