"""The guardrails, in code.

Seven rules, each about one thing:

``path-confinement``   no path argument may leave the repository.
``protected-paths``    the bootstrap, policies, verifier and config may only be
                       written under an amendment.
``journal-integrity``  nothing may write into the runtime's own history.
``budget``             a run has a finite number of steps.
``payload-size``       no single write may exceed a sane size.
``command-allowlist``  ``run_command`` may only run what the config allows.
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

from .base import Policy, ReviewContext, Verdict


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
    title = "Guardrail files need an amendment"
    description = (
        "Mutating skills may not write to the bootstrap, the policy engine, the "
        "verifier or ourob.toml unless an active amendment authorises that path."
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
                relpath = fsx.rel(target, ctx.repo)
            except PathEscapeError:
                continue  # path-confinement owns that failure
            if not ctx.config.is_protected(relpath):
                continue
            amendment = ctx.amendment_for(relpath)
            if amendment is not None:
                authorised.append(f"{relpath} ({amendment.amendment_id})")
            elif relpath in ctx.amended_paths():
                authorised.append(relpath)
            else:
                offenders.append(relpath)
        if offenders:
            return Verdict.deny(
                self.name,
                "protected path(s) require a ratified amendment: "
                + ", ".join(offenders)
                + " (use the propose_amendment skill, then `ourob ratify`)",
            )
        if authorised:
            return Verdict.allow(self.name, f"amendment covers {', '.join(authorised)}")
        return Verdict.allow(self.name)


class JournalIntegrityPolicy(Policy):
    name = "journal-integrity"
    title = "History is append-only"
    description = (
        "No skill may write, edit or delete anything under .ourob/journal, "
        ".ourob/index.jsonl or .ourob/verify -- the record of what the runtime "
        "did must be harder to alter than the thing it records."
    )
    blocking = True

    FORBIDDEN = (".ourob/journal", ".ourob/index.jsonl", ".ourob/verify")

    def review(self, ctx: ReviewContext) -> Verdict:
        mutating = bool(ctx.services.get("mutating_skills", {}).get(ctx.skill, False))
        if not mutating:
            return Verdict.allow(self.name)
        from .. import fsx
        from ..errors import PathEscapeError

        for _key, value in ctx.path_args():
            try:
                relpath = fsx.rel(fsx.confine(ctx.repo, value), ctx.repo)
            except PathEscapeError:
                continue
            for forbidden in self.FORBIDDEN:
                if relpath == forbidden or relpath.startswith(forbidden.rstrip("/") + "/"):
                    return Verdict.deny(
                        self.name, f"{relpath} is part of the runtime's history and is immutable"
                    )
        return Verdict.allow(self.name)


class BudgetPolicy(Policy):
    name = "budget"
    title = "Runs are finite"
    description = "A run may not exceed its configured step budget."
    blocking = True

    def review(self, ctx: ReviewContext) -> Verdict:
        if ctx.budget_used >= ctx.budget_limit:
            return Verdict.deny(
                self.name, f"step budget exhausted ({ctx.budget_used}/{ctx.budget_limit})"
            )
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
            return Verdict.deny(
                self.name, f"{joined!r} is not on the allowlist ({', '.join(allowed)})"
            )
        return Verdict.allow(self.name)


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
        LoopBreakerPolicy,
    )
}


def default_policy_set() -> PolicySet:  # noqa: F821 - resolved at import time below
    from .base import PolicySet

    return PolicySet(cls() for cls in POLICY_CLASSES.values())
