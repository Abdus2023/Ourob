#!/usr/bin/env python3
"""ourob bootstrap -- the smallest thing that can rebuild the runtime.

This file is the root of trust.  It is deliberately stdlib-only, deliberately
small, and deliberately does **not** import :mod:`ourob` until after it has
checked the repository against ``bootstrap.lock.json``.  If the runtime's own
code cannot be trusted, the bootstrap has to be able to say so using nothing but
the standard library and the lock file.

Usage::

    python bootstrap.py                     verify, then hand off to the CLI
    python bootstrap.py <ourob subcommand>  verify, then run `ourob <subcommand>`
    python bootstrap.py --prove             print the integrity proof and stop
    python bootstrap.py --rebuild           rewrite the lock from the working tree
    python bootstrap.py --strict            fail on any drift, protected or not
    python bootstrap.py --trust-drift       proceed despite protected drift

The verification done here is an independent implementation of the same hash
walk the library performs.  That duplication is the point: the checker and the
thing being checked are not the same code.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
LOCK = REPO / "bootstrap.lock.json"
CONFIG = REPO / "ourob.toml"

#: Directories that are never part of the runtime's self-description.
EXCLUDES = {
    ".git",
    ".ourob",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
    "node_modules",
    "dist",
    "build",
}

#: Hard-coded floor.  Even if ourob.toml is edited to protect nothing, the
#: bootstrap will not run a tree whose own bootstrap has drifted.
IMMUTABLE = ("bootstrap.py", "bootstrap.lock.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def walk() -> list[str]:
    import os

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDES and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if (
                name.endswith((".pyc", ".pyo"))
                or name.endswith("~")
                or name == ".coverage"
                or name.startswith(".coverage.")
            ):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(REPO).as_posix()
            if rel == "bootstrap.lock.json":
                continue
            found.append(rel)
    return found


def read_protected() -> list[str]:
    """Parse the protected list out of ourob.toml without a TOML library."""
    if not CONFIG.is_file():
        return list(IMMUTABLE)
    protected: list[str] = []
    inside_policy = False
    inside_list = False
    for raw in CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            inside_policy = line == "[policy]"
            inside_list = False
            continue
        if inside_policy and line.startswith("protected"):
            inside_list = "[" in line
            chunk = line.split("=", 1)[1] if "=" in line else ""
            protected.extend(_extract(chunk))
            if "]" in line:
                inside_list = False
            continue
        if inside_policy and inside_list:
            protected.extend(_extract(line))
            if "]" in line:
                inside_list = False
    for entry in IMMUTABLE:
        if entry not in protected:
            protected.append(entry)
    return protected


def _extract(chunk: str) -> list[str]:
    chunk = chunk.replace("[", " ").replace("]", " ")
    return [
        piece.strip().strip('"').strip("'")
        for piece in chunk.split(",")
        if piece.strip().strip('"').strip("'")
    ]


def is_protected(rel: str, protected: list[str]) -> bool:
    for pattern in protected:
        pattern = pattern.strip()
        if pattern.endswith("/"):
            if rel.startswith(pattern) or rel + "/" == pattern:
                return True
        elif rel == pattern:
            return True
    return False


def verify(strict: bool = False) -> tuple[bool, dict[str, object]]:
    """Compare the tree against the lock.  Returns (trusted, detail)."""
    protected = read_protected()
    if not LOCK.is_file():
        return False, {
            "lock": None,
            "error": "bootstrap.lock.json is missing; the tree is unanchored",
            "protected": protected,
        }
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    expected = {entry["path"]: entry["sha256"] for entry in data.get("files", [])}

    present = walk()
    added = sorted(set(present) - set(expected))
    removed = sorted(set(expected) - set(present))
    changed = sorted(
        p for p in set(expected) & set(present) if sha256_file(REPO / p) != expected[p]
    )

    drift = added + changed + removed
    violations = sorted({p for p in drift if is_protected(p, protected)})
    trusted = not violations and (not drift if strict else True)
    return trusted, {
        "lock": data.get("digest", ""),
        "version": data.get("version", 1),
        "files": len(expected),
        "protected": protected,
        "added": added,
        "changed": changed,
        "removed": removed,
        "violations": violations,
        "drift": drift,
        "trusted": trusted,
        "created_at": data.get("created_at", ""),
        "notes": data.get("notes", ""),
    }


def rebuild(note: str) -> dict[str, object]:
    """Rewrite the lock from the working tree using only the standard library."""
    protected = read_protected()
    previous = {}
    version = 0
    if LOCK.is_file():
        try:
            previous = json.loads(LOCK.read_text(encoding="utf-8"))
            version = int(previous.get("version", 0))
        except (json.JSONDecodeError, ValueError):
            previous = {}
    files = []
    for rel in sorted(walk()):
        path = REPO / rel
        files.append(
            {
                "path": rel,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "protected": is_protected(rel, protected),
            }
        )
    body = "\n".join(f"{f['path']} {f['sha256']}" for f in files)
    payload = {
        "format": "ourob.bootstrap.lock/v1",
        "version": version + 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notes": note or previous.get("notes", "") or "rebuilt by bootstrap.py",
        "digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "files": files,
    }
    tmp = LOCK.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(LOCK)
    return {"files": len(files), "digest": payload["digest"], "version": payload["version"]}


def prove(detail: dict[str, object]) -> str:
    lines = [
        f"repository   {REPO}",
        f"python       {sys.executable} ({sys.version.split()[0]})",
        f"lock digest  {str(detail.get('lock') or '(none)')[:64]}",
        f"lock version {detail.get('version', '-')}   files {detail.get('files', '-')}",
        f"created      {detail.get('created_at', '-')}",
        f"protected    {len(detail.get('protected') or [])} patterns",
    ]
    if detail.get("error"):
        lines.append(f"ERROR        {detail['error']}")
    for label in ("added", "changed", "removed"):
        items = detail.get(label) or []
        for item in items:
            lines.append(f"  {label:<8} {item}")
    violations = detail.get("violations") or []
    if violations:
        lines.append(f"VIOLATIONS   {', '.join(str(v) for v in violations)}")
    lines.append(f"verdict      {'TRUSTED' if detail.get('trusted') else 'NOT TRUSTED'}")
    return "\n".join(lines)


def ensure_importable() -> Path:
    source_root = REPO / "src"
    if not (source_root / "ourob" / "__init__.py").is_file():
        source_root = REPO
    entry = str(source_root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return source_root


#: Flags the bootstrap consumes itself. Everything else is handed to the CLI
#: untouched -- including the values that follow its own options.
BOOT_FLAGS = ("--prove", "--strict", "--trust-drift", "--explain")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `--rebuild` re-anchors the lock, but only when it is the first argument;
    # otherwise it belongs to whatever subcommand follows (`manifest --rebuild`).
    if argv and argv[0] == "--rebuild":
        outcome = rebuild(" ".join(argv[1:]) or "rebuilt by bootstrap.py --rebuild")
        print(
            f"lock rewritten: {outcome['files']} files, version {outcome['version']}, "
            f"digest {str(outcome['digest'])[:16]}"
        )
        return 0

    flags = {a for a in argv if a in BOOT_FLAGS}
    rest = [a for a in argv if a not in BOOT_FLAGS]

    trusted, detail = verify(strict="--strict" in flags)
    if "--prove" in flags:
        print(prove(detail))
        if "--trust-drift" not in flags and not trusted:
            return 1
        return 0

    if not trusted:
        if "--trust-drift" in flags:
            violations = ", ".join(str(v) for v in (detail.get("violations") or []))
            print(
                f"bootstrap: proceeding despite protected drift ({violations or 'unverified tree'})",
                file=sys.stderr,
            )
        else:
            print(prove(detail), file=sys.stderr)
            print(
                "\nrefusing to boot: protected paths have drifted from bootstrap.lock.json.\n"
                "  open an amendment  ->  python bootstrap.py amend <path> --why \"...\"\n"
                "  accept the drift   ->  python bootstrap.py --trust-drift <command>\n"
                "  re-anchor the tree ->  python bootstrap.py --rebuild",
                file=sys.stderr,
            )
            return 1

    source_root = ensure_importable()
    if "--explain" in flags:
        print(f"bootstrap: verified, importing ourob from {source_root}")
    try:
        from ourob.cli import main as cli_main
    except Exception as exc:  # pragma: no cover - only when the tree is broken
        print(f"bootstrap: verified the tree but could not import ourob: {exc}", file=sys.stderr)
        return 1

    if not rest:
        rest = ["doctor"]
    return cli_main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
