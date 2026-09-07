"""Cold start: booting the runtime from the repository alone.

This is the bootstrap mechanism.  It answers the question "what does it take to
get this runtime running again, given nothing but a checkout?" -- and the answer
is deliberately *nothing*: no install step, no registry, no network.  The
repository contains its own source, its own integrity record, and the ~40 lines
of code needed to trust that record and put itself on ``sys.path``.

The entry point is the top-level ``bootstrap.py``, which is stdlib-only and does
not import :mod:`ourob` until after it has verified the tree.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import fsx
from ..config import Config
from ..errors import BootstrapError
from .manifest import Manifest, ManifestDiff, compare

SRC_LAYOUT = "src"


@dataclass
class BootReport:
    repo: Path
    python: str
    source_root: Path
    manifest_digest: str = ""
    drift: ManifestDiff = field(default_factory=ManifestDiff)
    trusted: bool = False
    forced: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo.as_posix(),
            "python": self.python,
            "source_root": self.source_root.as_posix(),
            "manifest_digest": self.manifest_digest,
            "drift": self.drift.to_dict(),
            "trusted": self.trusted,
            "forced": self.forced,
            "notes": self.notes,
        }

    def describe(self) -> str:
        lines = [
            f"repo        {self.repo}",
            f"python      {self.python}",
            f"source root {self.source_root}",
            f"lock digest {self.manifest_digest[:16] or '(none)'}",
            f"drift       {self.drift.describe()}",
            f"trusted     {self.trusted}{' (forced)' if self.forced else ''}",
        ]
        lines.extend(f"  - {note}" for note in self.notes)
        return "\n".join(lines)


def locate_source_root(repo: Path) -> Path:
    """``src/`` if the project uses a src layout, else the repo root."""
    candidate = Path(repo) / SRC_LAYOUT
    if (candidate / "ourob" / "__init__.py").is_file():
        return candidate
    if (Path(repo) / "ourob" / "__init__.py").is_file():
        return Path(repo)
    raise BootstrapError(f"cannot find the ourob package under {repo}")


def ensure_importable(repo: Path) -> Path:
    """Put the source root on ``sys.path`` and return it."""
    root = locate_source_root(repo)
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return root


def protected_drift(diff: ManifestDiff, config: Config) -> list[str]:
    changed: set[str] = set(diff.added) | set(diff.changed) | set(diff.removed) | set(diff.missing)
    return sorted(p for p in changed if config.is_protected(p))


def boot(
    repo: Path | None = None,
    *,
    trust_drift: bool = False,
    rebuild_lock: bool = False,
    import_runtime: bool = True,
) -> tuple[Any, BootReport]:
    """Verify the tree, make it importable, and import the runtime.

    Returns ``(ourob_module, report)``.  Raises :class:`BootstrapError` when the
    tree does not match its lock on a protected path and *trust_drift* is false.
    """
    repo = Path(repo or fsx.repo_root()).resolve()
    config = Config.load(repo)
    source_root = ensure_importable(repo)

    report = BootReport(
        repo=repo,
        python=sys.executable,
        source_root=source_root,
    )

    lock_path = repo / "bootstrap.lock.json"
    manifest: Manifest | None = None
    if lock_path.is_file():
        manifest = Manifest.load(repo)
        report.manifest_digest = manifest.digest
        report.drift = compare(manifest, repo)
        violations = protected_drift(report.drift, config)
        if violations and not trust_drift and not rebuild_lock:
            raise BootstrapError(
                "protected paths have drifted from bootstrap.lock.json: "
                + ", ".join(violations)
                + "\n  open an amendment (`ourob amend`) or pass --trust-drift to proceed."
            )
        if violations:
            report.notes.append(
                f"protected drift accepted: {', '.join(violations)}"
            )
        if not report.drift.clean:
            report.notes.append(f"untracked/engineered drift: {report.drift.describe()}")
    else:
        report.notes.append("no bootstrap.lock.json yet; the tree is unanchored")

    report.forced = trust_drift or rebuild_lock

    if rebuild_lock:
        manifest = Manifest.build(
            repo,
            notes="rebuilt by `bootstrap --rebuild`",
            version=(manifest.version + 1) if manifest else 1,
        )
        path = manifest.save()
        report.manifest_digest = manifest.digest
        report.drift = ManifestDiff()
        report.notes.append(f"lock rewritten ({len(manifest)} files) -> {fsx.rel(path, repo)}")
        report.trusted = True
    elif manifest is None:
        # Nothing to check against.  The runtime will still run, but the tree is
        # not a *known* version of itself.
        report.trusted = False
    else:
        # We got here without raising, so no protected path has unauthorised
        # drift.  Ordinary engineering drift is expected and still trusted.
        report.trusted = True

    module = None
    if import_runtime:
        # Deliberately does not purge sys.modules: re-importing a runtime that is
        # already running would give the caller two distinct sets of classes.
        try:
            module = importlib.import_module("ourob")
        except Exception as exc:  # pragma: no cover - depends on the tree being broken
            raise BootstrapError(
                f"the tree passed verification but `import ourob` failed: {exc}"
            ) from exc
        report.notes.append(f"imported ourob {getattr(module, '__version__', '?')}")

    return module, report
