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

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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


def _run(argv: list[str], repo: Path, timeout: int, stdin: str = "") -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=repo,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(repo),
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"executable not found: {argv[0]!r}"
    return proc.returncode, proc.stdout, proc.stderr


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
        rc, out, err = _run(argv, ctx.repo, int(kwargs.get("timeout", 300)))
        text = _bundle(out, err)
        return SkillResult(
            ok=rc == 0,
            output=text or f"(exit {rc}, no output)",
            data={"argv": argv, "exit_code": rc, "timed_out": rc == 124},
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
        )
        text = _bundle(out, err)
        return SkillResult(
            ok=rc == 0,
            output=text or f"(exit {rc}, no output)",
            data={"exit_code": rc, "chars": len(code), "timed_out": rc == 124},
            error=None if rc == 0 else f"exit code {rc}",
        )
