"""The guardrails, in code.

Eight rules, each about one thing:

``path-confinement``   no path argument may leave the repository.
``protected-paths``    the bootstrap, policies, verifier and config may only be
                       written under a separate current operator grant.
``journal-integrity``  nothing may write into the runtime's own history.
``budget``             a run has a finite number of steps.
``payload-size``       no single write may exceed a sane size.
``command-allowlist``  ``run_command`` may only run what the config allows.
``child-filesystem``   child code is refused unless a write boundary is available.
``loop-breaker``       the same failing call may not be retried forever.

Path confinement no longer guesses.  A skill declares which of its parameters
carry paths (``"path": true`` in its schema) and :class:`PathConfinementPolicy`
confines exactly those; the conventional key names remain as a net underneath.
A string parameter with a path-like name that does *not* declare itself is
rejected statically by the ``skill-contract`` gate, so the hole is closed at
registration time rather than warned about at call time.

These live in a protected directory.  Changing them is a constitutional act.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import Policy, ReviewContext, Verdict


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _lexical_repo_rel(repo: Path, value: str) -> str | None:
    """Return the normalized spelling under repo without resolving symlinks."""
    root = Path(repo).resolve()
    candidate = Path(value)
    joined = candidate if candidate.is_absolute() else root / candidate
    lexical = Path(os.path.abspath(joined))
    try:
        return lexical.relative_to(root).as_posix()
    except ValueError:
        return None


def _protected_logical_paths(ctx: ReviewContext, value: str, target: Path) -> list[str]:
    """Map a real target back to every protected logical path that reaches it.

    Checking only the resolved target misses a protected path whose own symlink
    points into an unprotected directory. Checking only the supplied spelling
    misses aliases to protected files. Both directions matter for mutation.
    """
    repo = Path(ctx.repo).resolve()
    real_target = Path(target).resolve(strict=False)
    try:
        real_rel = real_target.relative_to(repo).as_posix()
    except ValueError:
        return []

    matches: set[str] = set()
    lexical_rel = _lexical_repo_rel(repo, value)
    if lexical_rel is not None and ctx.config.is_protected(lexical_rel):
        matches.add(lexical_rel)
    if ctx.config.is_protected(real_rel):
        matches.add(real_rel)

    for raw in ctx.config.policy.protected:
        pattern = str(raw).replace("\\", "/").strip()
        directory = pattern.endswith("/")
        logical_root = pattern.rstrip("/")
        if (
            not logical_root
            or logical_root.startswith("/")
            or any(part == ".." for part in logical_root.split("/"))
        ):
            continue
        resolved_root = (repo / logical_root).resolve(strict=False)
        if not _within(resolved_root, repo) or not _within(real_target, resolved_root):
            continue
        if directory:
            suffix = real_target.relative_to(resolved_root).as_posix()
            matches.add(logical_root if suffix == "." else f"{logical_root}/{suffix}")
        elif real_target == resolved_root:
            matches.add(logical_root)

    return sorted(matches)


class PathConfinementPolicy(Policy):
    name = "path-confinement"
    title = "Paths stay inside the repository"
    description = (
        "Every argument the skill declared as a path (plus the conventional key "
        "names) must resolve inside the repository root. Traversal and absolute "
        "escapes are refused before the skill runs."
    )
    blocking = True

    def review(self, ctx: ReviewContext) -> Verdict:
        from ..errors import PathEscapeError
        from ..fsx import confine

        for key, value in ctx.path_args():
            try:
                confine(ctx.repo, value)
            except PathEscapeError as exc:
                return Verdict.deny(self.name, f"{key}={value!r}: {exc}")
        return Verdict.allow(self.name)


class ProtectedPathPolicy(Policy):
    name = "protected-paths"
    title = "Guardrail files need a separate operator grant"
    description = (
        "Mutating skills may not write protected paths unless an integrity-checked "
        "system-journal grant matches the current proposal, revision, digest, and path."
    )
    blocking = True

    def review(self, ctx: ReviewContext) -> Verdict:
        mutating = bool(ctx.services.get("mutating_skills", {}).get(ctx.skill, False))
        if not mutating:
            return Verdict.allow(self.name, "read-only skill")
        from .. import fsx
        from ..errors import PathEscapeError

        offenders: list[str] = []
        authorised: list[str] = []
        for _key, value in ctx.path_args():
            try:
                target = fsx.confine(ctx.repo, value)
            except PathEscapeError:
                continue  # path-confinement owns that failure
            logical_paths = _protected_logical_paths(ctx, value, target)
            if not logical_paths:
                continue
            grant = next(
                (
                    (logical, amendment)
                    for logical in logical_paths
                    if (amendment := ctx.amendment_for(logical)) is not None
                ),
                None,
            )
            if grant is not None:
                logical, amendment = grant
                authorised.append(f"{logical} ({amendment.amendment_id})")
            else:
                offenders.extend(logical_paths)
        if offenders:
            return Verdict.deny(
                self.name,
                "protected path(s) require a separately authorized amendment: "
                + ", ".join(offenders)
                + " (a proposal is not authority; an operator must use `ourob authorize`, then promote)",
            )
        if authorised:
            return Verdict.allow(self.name, f"amendment covers {', '.join(authorised)}")
        return Verdict.allow(self.name)


class JournalIntegrityPolicy(Policy):
    name = "journal-integrity"
    title = "History is append-only"
    description = (
        "No ordinary mutating skill may write, edit, or delete runtime state under "
        ".ourob, including run journals, .head anchors, promotion history, and "
        "amendment proposals. Child processes receive an OS write boundary as well."
    )
    blocking = True

    def review(self, ctx: ReviewContext) -> Verdict:
        mutating = bool(ctx.services.get("mutating_skills", {}).get(ctx.skill, False))
        if not mutating:
            return Verdict.allow(self.name)
        from .. import fsx
        from ..errors import PathEscapeError

        state_root = (Path(ctx.repo) / ".ourob").resolve(strict=False)
        for _key, value in ctx.path_args():
            try:
                target = fsx.confine(ctx.repo, value)
            except PathEscapeError:
                continue
            lexical = _lexical_repo_rel(ctx.repo, value)
            lexical_state_path = lexical == ".ourob" or bool(lexical and lexical.startswith(".ourob/"))
            resolved_state_path = _within(target, state_root)
            if lexical_state_path or resolved_state_path:
                relpath = lexical if lexical_state_path else fsx.rel(target, ctx.repo)
                return Verdict.deny(
                    self.name, f"{relpath} resolves into the runtime's history and is immutable"
                )
        return Verdict.allow(self.name)


class BudgetPolicy(Policy):
    name = "budget"
    title = "Runs are finite"
    description = "A run may not exceed its configured step budget."
    blocking = True

    def review(self, ctx: ReviewContext) -> Verdict:
        if ctx.budget_used >= ctx.budget_limit:
            return Verdict.deny(self.name, f"step budget exhausted ({ctx.budget_used}/{ctx.budget_limit})")
        return Verdict.allow(self.name, f"{ctx.budget_used}/{ctx.budget_limit} steps used")


class PayloadSizePolicy(Policy):
    name = "payload-size"
    title = "Writes are bounded"
    description = "No single write or edit may exceed the configured byte limit."
    blocking = True

    def review(self, ctx: ReviewContext) -> Verdict:
        limit = int(ctx.config.policy.max_file_bytes)
        for key in ("content", "new_text", "code"):
            value = ctx.args.get(key)
            if isinstance(value, str) and len(value.encode("utf-8")) > limit:
                return Verdict.deny(
                    self.name, f"{key} is {len(value.encode('utf-8'))} bytes, limit is {limit}"
                )
        return Verdict.allow(self.name)


class CommandAllowlistPolicy(Policy):
    name = "command-allowlist"
    title = "Commands come from the allowlist"
    description = (
        "run_command may only execute command lines that begin with an entry in "
        "[policy].allow_commands and contain no deny pattern."
    )
    blocking = True

    def review(self, ctx: ReviewContext) -> Verdict:
        if ctx.skill != "run_command":
            return Verdict.allow(self.name)
        argv = ctx.args.get("argv")
        if not isinstance(argv, list) or not argv:
            return Verdict.deny(self.name, "run_command requires a non-empty argv list")
        joined = " ".join(str(a) for a in argv)
        for pattern in ctx.config.policy.deny_patterns:
            if pattern and pattern in joined:
                return Verdict.deny(self.name, f"matches deny pattern {pattern!r}")
        allowed = ctx.config.policy.allow_commands
        if allowed and not any(joined == a or joined.startswith(a + " ") for a in allowed):
            return Verdict.deny(self.name, f"{joined!r} is not on the allowlist ({', '.join(allowed)})")
        return Verdict.allow(self.name)


class ChildFilesystemPolicy(Policy):
    name = "child-filesystem"
    title = "Child content writes require a filesystem boundary"
    description = (
        "Child-process skills require Linux Landlock restrictions to deny content "
        "and namespace writes under protected paths, runtime state, and Git metadata. "
        "File-mode and timestamp changes are not confined."
    )
    blocking = True

    CHILD_SKILLS = {"run_command", "run_python", "run_tests", "run_verification"}

    def review(self, ctx: ReviewContext) -> Verdict:
        if ctx.skill not in self.CHILD_SKILLS:
            return Verdict.allow(self.name, "skill does not execute child code")
        from ..child_sandbox import landlock_status

        status = landlock_status()
        if not status.available:
            return Verdict.deny(
                self.name,
                "refusing child execution because filesystem write confinement is unavailable: "
                + status.reason,
            )
        return Verdict.allow(
            self.name,
            f"Landlock ABI {status.abi}: content/namespace writes to protected paths, "
            ".ourob, and .git are denied; mode/time changes are not confined",
        )


class LoopBreakerPolicy(Policy):
    name = "loop-breaker"
    title = "Failing calls are not retried forever"
    description = (
        "The same invocation may fail at most three times in a row; after that the "
        "runtime must try something different rather than burn its budget."
    )
    blocking = True
    MAX_REPEATS = 3

    def review(self, ctx: ReviewContext) -> Verdict:
        failures = ctx.recent_failures()
        if failures >= self.MAX_REPEATS:
            return Verdict.deny(
                self.name,
                f"this exact call has failed {failures} times; change the approach",
            )
        return Verdict.allow(self.name)


POLICY_CLASSES: dict[str, type[Policy]] = {
    cls.name: cls
    for cls in (
        PathConfinementPolicy,
        ProtectedPathPolicy,
        JournalIntegrityPolicy,
        BudgetPolicy,
        PayloadSizePolicy,
        CommandAllowlistPolicy,
        ChildFilesystemPolicy,
        LoopBreakerPolicy,
    )
}


def default_policy_set() -> PolicySet:  # noqa: F821 - resolved at import time below
    from .base import PolicySet

    return PolicySet(cls() for cls in POLICY_CLASSES.values())
