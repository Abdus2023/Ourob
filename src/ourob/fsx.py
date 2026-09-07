"""Filesystem primitives: confinement, hashing, atomic writes.

Every skill that touches disk goes through :func:`confine` first.  There is no
alternate path in the codebase that resolves a user-supplied path.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from .errors import PathEscapeError

REPO_MARKER = "ourob.toml"

DEFAULT_EXCLUDES = frozenset(
    {
        ".git",
        ".ourob",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
        ".egg-info",
    }
)


def repo_root(start: Path | None = None) -> Path:
    """Walk up from *start* looking for :data:`REPO_MARKER`."""
    cursor = (start or Path.cwd()).resolve()
    for candidate in (cursor, *cursor.parents):
        if (candidate / REPO_MARKER).is_file():
            return candidate
    raise PathEscapeError(
        f"no {REPO_MARKER!r} at or above {cursor}; refusing to guess a repository root"
    )


def confine(root: Path, rel: str | os.PathLike[str]) -> Path:
    """Resolve *rel* inside *root*, refusing anything that escapes.

    Rejects ``..`` traversal, absolute paths outside the root, and symlinks whose
    target resolves outside the root.
    """
    root_real = os.path.realpath(root)
    candidate = Path(rel)
    joined = candidate if candidate.is_absolute() else Path(root_real) / candidate
    real = os.path.realpath(joined)
    if real != root_real and not real.startswith(root_real + os.sep):
        raise PathEscapeError(f"{rel!r} resolves to {real}, outside {root_real}")
    for part in Path(os.path.relpath(real, root_real)).parts:
        if part == os.pardir:  # pragma: no cover - defensive
            raise PathEscapeError(f"{rel!r} escapes the repository root")
    return Path(real)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def atomic_write(path: Path, data: str | bytes, *, mode: int = 0o644) -> None:
    """Write *data* so a crash mid-write cannot leave a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ourob-tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        discard(tmp)
        raise


def discard(path: str | Path) -> None:
    """Best-effort unlink, for cleaning up after a failed atomic write."""
    with contextlib.suppress(OSError):
        os.unlink(path)


def walk_repo(
    root: Path, *, excludes: Iterable[str] = DEFAULT_EXCLUDES
) -> Iterator[Path]:
    """Yield every candidate file in the repository, deterministically ordered."""
    exclude = set(excludes)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in exclude and not d.endswith(".egg-info")
        )
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")) or name.endswith("~"):
                continue
            yield Path(dirpath) / name


def rel(path: Path, root: Path) -> str:
    """Repository-relative POSIX path."""
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
