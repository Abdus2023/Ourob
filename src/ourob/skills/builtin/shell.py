"""Execution skills.

Two ways to run code, both sandboxed to the repository directory with a
stripped environment and a hard timeout:

``run_command``  an allowlisted shell command (``python``, ``pytest``, ``git
                 status`` ...).  The allowlist lives in ``ourob.toml`` and the
                 shell is never invoked -- the command line is split and passed
                 straight to ``execvp``, so there is nothing to inject into.
``run_python``   a snippet of Python, run in a child interpreter with
                 ``PYTHONPATH`` pointing at the repository's own ``src``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...config import ResourceLimits
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


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill the child's whole process group, not just the direct child."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _run(
    argv: list[str],
    repo: Path,
    timeout: int,
    stdin: str = "",
    limits: ResourceLimits | None = None,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.Popen(
            argv,
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_child_env(repo),
            preexec_fn=_limit_preexec(limits),
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "", f"executable not found: {argv[0]!r}"
    except OSError as exc:
        return 126, "", f"could not start {argv[0]!r}: {exc}"
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return 124, out, f"timed out after {timeout}s"
    return proc.returncode, out, err


def _limits(ctx: SkillContext) -> ResourceLimits | None:
    config = ctx.services.get("config")
    return getattr(getattr(config, "policy", None), "limits", None)


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
        "Run a shell-free command from the allowlist in ourob.toml inside the "
        "repository. No shell is involved, so arguments cannot escape."
    ),
    params={
        "argv": {
            "type": "list",
            "required": True,
            "desc": 'command and arguments, e.g. ["python", "-m", "pytest", "-q"]',
        },
        "timeout": {"type": "int", "default": 300, "min": 1, "max": 3600},
    },
)
class RunCommand(Skill):
    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        argv = [str(a) for a in kwargs["argv"]]
        if not argv:
            raise SkillError("argv must not be empty")
        config = ctx.services.get("config")
        allowed = list(getattr(config.policy, "allow_commands", [])) if config else []
        denied = list(getattr(config.policy, "deny_patterns", [])) if config else []
        joined = " ".join(argv)
        for pattern in denied:
            if pattern and pattern in joined:
                raise SkillError(f"command matches deny pattern {pattern!r}")
        if allowed and not any(joined == a or joined.startswith(a + " ") for a in allowed):
            raise SkillError(
                f"{argv[0]!r} is not on the allowlist; permitted prefixes: {', '.join(allowed)}"
            )
        for arg in argv:
            for token in (";", "&&", "||", "|", "`", "$(", ">", "<", "\n"):
                if token in arg:
                    raise SkillError(
                        f"shell metacharacter {token!r} is not permitted in argv ({arg!r})"
                    )
        rc, out, err = _run(
            argv, ctx.repo, int(kwargs.get("timeout", 300)), limits=_limits(ctx)
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
            },
            error=None if rc == 0 else f"exit code {rc}",
        )


@skill(
    "run_python",
    title="Run Python",
    description=(
        "Execute a Python snippet in a child interpreter with the repository's own "
        "src on PYTHONPATH and the user site disabled. Returns stdout, stderr and "
        "the exit code."
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
        rc, out, err = _run(
            [sys.executable, "-s", "-c", code],
            ctx.repo,
            int(kwargs.get("timeout", 120)),
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
            },
            error=None if rc == 0 else f"exit code {rc}",
        )
