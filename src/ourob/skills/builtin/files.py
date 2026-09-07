"""File skills: the runtime's hands inside its own repository.

Every path argument is funnelled through :meth:`SkillContext.path`, which
confines it to the repository root.  Writes are atomic.  Nothing here knows
about policies -- confinement is enforced by :func:`ourob.fsx.confine` and the
policy layer decides whether the call happens at all.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ... import fsx
from ...errors import SkillError
from ...state.model import SkillResult
from ..base import Skill, SkillContext, skill

MAX_READ_BYTES = 400_000


@skill(
    "read_file",
    title="Read a file",
    description="Read a UTF-8 file from the repository and return its text.",
    params={
        "path": {"type": "str", "required": True, "path": True,
                 "desc": "repository-relative path"},
        "max_bytes": {
            "type": "int",
            "default": MAX_READ_BYTES,
            "min": 1,
            "desc": "truncate the read at this many bytes",
        },
    },
)
class ReadFile(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        target = ctx.path(kwargs["path"])
        if not target.is_file():
            raise SkillError(f"{kwargs['path']!r} is not a file")
        data = target.read_bytes()[: int(kwargs.get("max_bytes", MAX_READ_BYTES))]
        text = data.decode("utf-8", errors="replace")
        return SkillResult(
            ok=True,
            output=text,
            data={
                "path": fsx.rel(target, ctx.repo),
                "bytes": len(data),
                "sha256": fsx.sha256_bytes(data),
                "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            },
        )


@skill(
    "write_file",
    title="Write a file",
    description="Create or overwrite a file in the repository with the given text.",
    params={
        "path": {"type": "str", "required": True, "path": True,
                 "desc": "repository-relative path"},
        "content": {"type": "str", "required": True, "desc": "full file contents"},
    },
    mutating=True,
)
class WriteFile(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        relpath = kwargs["path"]
        target = ctx.path(relpath)
        content: str = kwargs["content"]
        existed = target.is_file()
        previous = target.read_bytes() if existed else b""
        fsx.atomic_write(target, content)
        normalised = fsx.rel(target, ctx.repo)
        ctx.log(f"wrote {normalised} ({len(content)} chars)")
        return SkillResult(
            ok=True,
            output=f"{'overwrote' if existed else 'created'} {normalised}",
            data={
                "path": normalised,
                "created": not existed,
                "bytes": len(content.encode("utf-8")),
                "sha256": fsx.sha256_text(content),
                "previous_sha256": fsx.sha256_bytes(previous) if existed else "",
            },
            artifacts=[normalised],
        )


@skill(
    "edit_file",
    title="Edit a file",
    description=(
        "Replace an exact substring in a file. The match must be unique unless "
        "occurrences is given."
    ),
    params={
        "path": {"type": "str", "required": True, "path": True},
        "old_text": {"type": "str", "required": True},
        "new_text": {"type": "str", "required": True},
        "occurrences": {"type": "int", "default": 1, "min": 1, "desc": "how many to replace"},
    },
    mutating=True,
)
class EditFile(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        relpath = kwargs["path"]
        target = ctx.path(relpath)
        if not target.is_file():
            raise SkillError(f"{relpath!r} is not a file")
        text = target.read_text(encoding="utf-8")
        old, new = kwargs["old_text"], kwargs["new_text"]
        if not old:
            raise SkillError("old_text must not be empty")
        found = text.count(old)
        limit = int(kwargs.get("occurrences", 1))
        if found == 0:
            raise SkillError(f"old_text not found in {relpath!r}")
        if found > limit:
            raise SkillError(
                f"old_text matches {found} times in {relpath!r}; "
                f"make it more specific or pass occurrences={found}"
            )
        updated = text.replace(old, new, limit)
        fsx.atomic_write(target, updated)
        normalised = fsx.rel(target, ctx.repo)
        return SkillResult(
            ok=True,
            output=f"edited {normalised}: replaced {found} occurrence(s)",
            data={
                "path": normalised,
                "replacements": found,
                "sha256": fsx.sha256_text(updated),
                "delta_bytes": len(updated.encode()) - len(text.encode()),
            },
            artifacts=[normalised],
        )


@skill(
    "delete_file",
    title="Delete a file",
    description="Remove a file from the repository.",
    params={"path": {"type": "str", "required": True, "path": True}},
    mutating=True,
)
class DeleteFile(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        relpath = kwargs["path"]
        target = ctx.path(relpath)
        if not target.is_file():
            raise SkillError(f"{relpath!r} is not a file")
        digest = fsx.sha256_file(target)
        target.unlink()
        normalised = fsx.rel(target, ctx.repo)
        return SkillResult(
            ok=True,
            output=f"deleted {normalised}",
            data={"path": normalised, "previous_sha256": digest},
            artifacts=[normalised],
        )


@skill(
    "list_dir",
    title="List a directory",
    description="List the entries of a directory in the repository.",
    params={
        "path": {"type": "str", "default": ".", "path": True,
                 "desc": "repository-relative directory"},
        "recursive": {"type": "bool", "default": False},
    },
)
class ListDir(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        relpath = kwargs.get("path") or "."
        target = ctx.path(relpath)
        if not target.is_dir():
            raise SkillError(f"{relpath!r} is not a directory")
        entries: list[dict[str, Any]] = []
        if kwargs.get("recursive"):
            for path in fsx.walk_repo(target):
                entries.append(
                    {"path": fsx.rel(path, ctx.repo), "type": "file", "bytes": path.stat().st_size}
                )
        else:
            for path in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
                entries.append(
                    {
                        "path": fsx.rel(path, ctx.repo),
                        "type": "dir" if path.is_dir() else "file",
                        "bytes": 0 if path.is_dir() else path.stat().st_size,
                    }
                )
        listing = "\n".join(
            f"{'d' if e['type'] == 'dir' else '-'} {e['bytes']:>8} {e['path']}" for e in entries
        )
        return SkillResult(
            ok=True, output=listing or "(empty)", data={"entries": entries, "count": len(entries)}
        )


@skill(
    "grep",
    title="Search the repository",
    description="Regular-expression search over text files in the repository.",
    params={
        "pattern": {"type": "str", "required": True},
        "path": {"type": "str", "default": ".", "path": True,
                 "desc": "file or directory to search"},
        "max_results": {"type": "int", "default": 100, "min": 1, "max": 2000},
        "ignore_case": {"type": "bool", "default": False},
    },
)
class Grep(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        flags = re.IGNORECASE if kwargs.get("ignore_case") else 0
        try:
            rx = re.compile(kwargs["pattern"], flags)
        except re.error as exc:
            raise SkillError(f"invalid regular expression: {exc}") from exc
        root = ctx.path(kwargs.get("path") or ".")
        targets: list[Path] = [root] if root.is_file() else list(fsx.walk_repo(root))
        limit = int(kwargs.get("max_results", 100))
        matches: list[dict[str, Any]] = []
        for path in targets:
            if len(matches) >= limit:
                break
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    matches.append(
                        {"path": fsx.rel(path, ctx.repo), "line": lineno, "text": line.strip()[:300]}
                    )
                    if len(matches) >= limit:
                        break
        output = "\n".join(f"{m['path']}:{m['line']}: {m['text']}" for m in matches)
        return SkillResult(
            ok=True,
            output=output or "(no matches)",
            data={"matches": matches, "count": len(matches), "truncated": len(matches) >= limit},
        )
