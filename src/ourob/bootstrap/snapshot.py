"""Working-tree snapshots: the rollback half of self-modification.

Before a run is allowed to touch anything, the kernel captures the bytes of every
file named in the bootstrap lock into ``.ourob/snapshots/<run_id>/``.  If
verification fails at promotion time, :meth:`Snapshot.restore` puts those bytes
back and deletes anything the run created that the lock never knew about.

This is deliberately not git.  Git is a fine place to *record* a ratified change,
but rollback has to work in a bare checkout with no history, and it has to be
exact about untracked files a self-modifying process just invented.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import fsx
from ..clock import stamp
from ..errors import BootstrapError
from .manifest import LOCK_NAME, Manifest, compare

SNAPSHOT_DIR = ".ourob/snapshots"


@dataclass
class Snapshot:
    repo: Path
    root: Path
    run_id: str
    created_at: str = field(default_factory=stamp)
    files: int = 0
    digest: str = ""

    @classmethod
    def capture(cls, repo: Path, run_id: str) -> Snapshot:
        repo = Path(repo).resolve()
        if "/" in run_id or run_id in {".", ".."}:
            raise BootstrapError(f"unsafe run id {run_id!r}")
        manifest = Manifest.load(repo)
        root = (repo / SNAPSHOT_DIR / run_id).resolve()
        if root.exists():
            raise BootstrapError(f"a snapshot for {run_id!r} already exists at {root}")
        root.mkdir(parents=True)
        for relpath in manifest.paths():
            source = repo / relpath
            if not source.is_file():
                continue
            target = root / "tree" / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        # The lock is not part of the manifest, but it is part of the state the
        # rollback has to reproduce.  Losing it would un-anchor the tree.
        lock = repo / LOCK_NAME
        if lock.is_file():
            (root / "lock").mkdir(parents=True, exist_ok=True)
            shutil.copy2(lock, root / "lock" / LOCK_NAME)
        snapshot = cls(
            repo=repo,
            root=root,
            run_id=run_id,
            files=len(manifest),
            digest=manifest.digest,
        )
        fsx.atomic_write(
            root / "snapshot.json",
            json.dumps(
                {
                    "run_id": run_id,
                    "created_at": snapshot.created_at,
                    "manifest_digest": manifest.digest,
                    "files": [manifest.files[p].to_dict() for p in manifest.paths()],
                },
                indent=2,
            )
            + "\n",
        )
        return snapshot

    @classmethod
    def load(cls, repo: Path, run_id: str) -> Snapshot:
        repo = Path(repo).resolve()
        root = (repo / SNAPSHOT_DIR / run_id).resolve()
        meta = root / "snapshot.json"
        if not meta.is_file():
            raise BootstrapError(f"no snapshot for run {run_id!r}")
        data = json.loads(meta.read_text(encoding="utf-8"))
        return cls(
            repo=repo,
            root=root,
            run_id=run_id,
            created_at=data.get("created_at", ""),
            files=len(data.get("files", [])),
            digest=data.get("manifest_digest", ""),
        )

    def restore(self) -> dict[str, Any]:
        """Put the tree back exactly as it was.  Returns what it did."""
        meta = self.root / "snapshot.json"
        data = json.loads(meta.read_text(encoding="utf-8"))
        expected = {f["path"] for f in data.get("files", [])}
        tree = self.root / "tree"

        restored: list[str] = []
        # The lock is restored first and is never a deletion candidate.
        captured_lock = self.root / "lock" / LOCK_NAME
        live_lock = self.repo / LOCK_NAME
        if captured_lock.is_file():
            wanted = captured_lock.read_bytes()
            current = live_lock.read_bytes() if live_lock.is_file() else b""
            if current != wanted:
                fsx.atomic_write(live_lock, wanted)
                restored.append(LOCK_NAME)

        for entry in data.get("files", []):
            relpath = entry["path"]
            backup = tree / relpath
            target = self.repo / relpath
            if not backup.is_file():
                continue
            current = fsx.sha256_file(target) if target.is_file() else ""
            if current != entry["sha256"]:
                fsx.atomic_write(target, backup.read_bytes())
                restored.append(relpath)

        removed: list[str] = []
        for path in list(fsx.walk_repo(self.repo)):
            relpath = fsx.rel(path, self.repo)
            if relpath.startswith(SNAPSHOT_DIR + "/") or relpath == LOCK_NAME:
                continue
            if relpath not in expected and path.is_file():
                path.unlink()
                removed.append(relpath)

        return {"restored": restored, "removed": removed}

    def discard(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @classmethod
    def list_for(cls, repo: Path) -> list[str]:
        root = Path(repo) / SNAPSHOT_DIR
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if (p / "snapshot.json").is_file())

    def drift_since(self) -> Any:
        """How far the tree has moved from this snapshot's manifest."""
        data = json.loads((self.root / "snapshot.json").read_text(encoding="utf-8"))
        manifest = Manifest(repo=self.repo)
        for entry in data.get("files", []):
            from .manifest import FileRecord

            manifest.files[entry["path"]] = FileRecord(
                path=entry["path"],
                sha256=entry["sha256"],
                size=int(entry.get("size", 0)),
                protected=bool(entry.get("protected", False)),
            )
        return compare(manifest, self.repo)
