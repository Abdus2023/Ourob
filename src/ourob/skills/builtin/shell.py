"""Execution skills.

Both child-process skills run with a stripped environment, a hard timeout,
resource limits, a Linux Landlock write boundary, and a narrow seccomp filter
for selected ownership/xattr changes. File-mode and timestamp changes are not
mediated. Children may create or modify file contents and namespace entries
only in unprotected repository directories and a per-call scratch directory.
This is not a general OS sandbox; systems without the required controls refuse
child execution.

``run_command``  an allowlisted command (``python``, ``pytest``, ``git status``).
                 The shell is never invoked; argv goes directly to the executable,
                 and the child inherits filesystem rules.
``run_python``   a Python snippet in a child interpreter with ``PYTHONPATH``
                 pointing at the repository's own ``src`` and the same rules.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...child_sandbox import run_child
from ...config import Config, ResourceLimits
from ...errors import SkillError
from ...state.model import SkillResult
from ..base import Skill, SkillContext, skill

MAX_OUTPUT_CHARS = 40_000


def _child_env(repo: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "PYTHONPATH": str(repo / "src"),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "OUROB_REPO": str(repo),
    }
    for key in ("OUROB_API_KEY", "OUROB_MODEL", "OUROB_BASE_URL"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _limit_preexec(limits: ResourceLimits | None) -> Callable[[], None] | None:
    """Build the pre-exec hook that caps the child before it runs any code.

    Runs after fork and before exec, so the child can never raise its own
    ceiling. ``start_new_session`` puts it in its own process group, which is
    what makes the timeout able to kill the whole tree rather than just the
    direct child -- otherwise a grandchild outlives the timeout.

    ``preexec_fn`` is unsafe in a multithreaded program; this runtime forks
    children from a single-threaded kernel loop, so the caveat does not bite.
    """
    if limits is None:
        return None
    try:
        import resource  # noqa: PLC0415  (POSIX only)
    except ImportError:
        return None

    wanted: list[tuple[int, int]] = []
    if limits.address_space_mb > 0:
        wanted.append((resource.RLIMIT_AS, limits.address_space_mb * 1024 * 1024))
    if limits.file_size_mb > 0:
        wanted.append((resource.RLIMIT_FSIZE, limits.file_size_mb * 1024 * 1024))
    if limits.cpu_seconds > 0:
        wanted.append((resource.RLIMIT_CPU, limits.cpu_seconds))
    if limits.processes > 0:
        wanted.append((resource.RLIMIT_NPROC, limits.processes))
    wanted.append((resource.RLIMIT_CORE, 0))
    if not wanted:
        return None

    def apply() -> None:
        for what, value in wanted:
            _, hard = resource.getrlimit(what)
            ceiling = value if hard == resource.RLIM_INFINITY else min(hard, value)
            resource.setrlimit(what, (ceiling, ceiling))

    return apply


def _run(
    argv: list[str],
    repo: Path,
    timeout: int,
    *,
    protected: list[str],
    stdin: str = "",
    limits: ResourceLimits | None = None,
) -> tuple[int, str, str]:
    return run_child(
        argv,
        repo=repo,
        protected=protected,
        env=_child_env(repo),
        timeout=timeout,
        stdin=stdin,
        limits=_limit_preexec(limits),
    )


def _config(ctx: SkillContext) -> Config:
    config = ctx.services.get("config")
    return config if isinstance(config, Config) else Config.load(ctx.repo)


def _limits(ctx: SkillContext) -> ResourceLimits | None:
    return _config(ctx).policy.limits


def _bundle(stdout: str, stderr: str) -> str:
    combined = stdout
    if stderr.strip():
        combined = f"{combined}\n--- stderr ---\n{stderr}".strip()
    if len(combined) > MAX_OUTPUT_CHARS:
        head = MAX_OUTPUT_CHARS // 2
        combined = (
            combined[:head]
            + f"\n... [{len(combined) - MAX_OUTPUT_CHARS} chars elided] ...\n"
            + combined[-head:]
        )
    return combined


@skill(
    "run_command",
    title="Run an allowlisted command",
    description=(
        "Run an allowlisted command without a shell. Linux Landlock blocks child "
        "content/namespace writes under protected paths, runtime state, and Git "
        "metadata; mode/timestamp changes are not confined. Unsupported hosts fail closed."
    ),
    params={
        "argv": {
            "type": "list",
            "required": True,
            "desc": 'command and arguments, e.g. ["python", "-m", "pytest", "-q"]',
        },
        "timeout": {"type": "int", "default": 300, "min": 1, "max": 3600},
    },
    mutating=True,
)
class RunCommand(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        argv = [str(a) for a in kwargs["argv"]]
        if not argv:
            raise SkillError("argv must not be empty")
        config = _config(ctx)
        allowed = list(config.policy.allow_commands)
        denied = list(config.policy.deny_patterns)
        joined = " ".join(argv)
        for pattern in denied:
            if pattern and pattern in joined:
                raise SkillError(f"command matches deny pattern {pattern!r}")
        if allowed and not any(joined == a or joined.startswith(a + " ") for a in allowed):
            raise SkillError(f"{argv[0]!r} is not on the allowlist; permitted prefixes: {', '.join(allowed)}")
        for arg in argv:
            for token in (";", "&&", "||", "|", "`", "$(", ">", "<", "\n"):
                if token in arg:
                    raise SkillError(f"shell metacharacter {token!r} is not permitted in argv ({arg!r})")
        rc, out, err = _run(
            argv,
            ctx.repo,
            int(kwargs.get("timeout", 300)),
            protected=list(config.policy.protected),
            limits=_limits(ctx),
        )
        text = _bundle(out, err)
        return SkillResult(
            ok=rc == 0,
            output=text or f"(exit {rc}, no output)",
            data={
                "argv": argv,
                "exit_code": rc,
                "timed_out": rc == 124,
                "limits": _limits(ctx).describe() if _limits(ctx) else "none",
                "filesystem_boundary": (
                    "Landlock content/namespace writes plus selected seccomp "
                    "ownership/xattr denial; mode/time changes not confined"
                ),
            },
            error=None if rc == 0 else f"exit code {rc}",
        )


@skill(
    "run_python",
    title="Run Python",
    description=(
        "Execute a Python snippet in a child interpreter. Landlock restricts child "
        "content/namespace writes to unprotected repository directories and a scratch "
        "tree; mode/timestamp changes are not confined. Unsupported hosts refuse execution."
    ),
    params={
        "code": {"type": "str", "required": True, "desc": "Python source to execute"},
        "timeout": {"type": "int", "default": 120, "min": 1, "max": 1800},
        "stdin": {"type": "str", "default": ""},
    },
    mutating=True,
)
class RunPython(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        code: str = kwargs["code"]
        if not code.strip():
            raise SkillError("code must not be empty")
        config = _config(ctx)
        rc, out, err = _run(
            [sys.executable, "-s", "-c", code],
            ctx.repo,
            int(kwargs.get("timeout", 120)),
            protected=list(config.policy.protected),
            stdin=kwargs.get("stdin", ""),
            limits=_limits(ctx),
        )
        text = _bundle(out, err)
        return SkillResult(
            ok=rc == 0,
            output=text or f"(exit {rc}, no output)",
            data={
                "exit_code": rc,
                "chars": len(code),
                "timed_out": rc == 124,
                "limits": _limits(ctx).describe() if _limits(ctx) else "none",
                "filesystem_boundary": (
                    "Landlock content/namespace writes plus selected seccomp "
                    "ownership/xattr denial; mode/time changes not confined"
                ),
            },
            error=None if rc == 0 else f"exit code {rc}",
        )
