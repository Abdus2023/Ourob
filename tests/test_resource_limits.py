"""Resource limits on the child processes the execution skills start.

A wall-clock timeout alone is not a sandbox. Generated code can exhaust memory
well before the timeout fires, fill the disk, or fork children that outlive it.
These tests exercise the actual kernel behaviour rather than the helper, by
running real children and observing what the kernel allowed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from ourob.config import Config, ResourceLimits
from ourob.skills.base import SkillContext
from ourob.skills.builtin.shell import RunCommand, RunPython, _kill_group, _limit_preexec


def ctx_with(repo: Path, **limits: int) -> SkillContext:
    config = Config.load(repo)
    config.policy.limits = ResourceLimits(**limits)
    return SkillContext(repo=repo, services={"config": config})


def run_python(repo: Path, code: str, **limits: int):
    return RunPython().run(ctx_with(repo, **limits), code=code, timeout=30)


# -- the limits reach the child -------------------------------------------


def test_the_child_sees_its_own_address_space_ceiling(repo: Path) -> None:
    result = run_python(
        repo,
        "import resource; print(resource.getrlimit(resource.RLIMIT_AS))",
        address_space_mb=256,
    )
    assert result.ok, result.output
    ceiling = 256 * 1024 * 1024
    assert str((ceiling, ceiling)) in result.output


def test_a_child_cannot_allocate_past_the_ceiling(repo: Path) -> None:
    result = run_python(
        repo,
        "b = bytearray(200 * 1024 * 1024); print('allocated', len(b))",
        address_space_mb=64,
    )
    assert not result.ok, f"a 200MB allocation must not succeed under a 64MB ceiling: {result.output}"
    assert "allocated 209715200" not in result.output


def test_a_child_cannot_write_past_the_file_size_ceiling(repo: Path) -> None:
    result = run_python(
        repo,
        "f = open('big.bin', 'wb'); f.write(bytearray(40 * 1024 * 1024)); f.close(); print('wrote it')",
        file_size_mb=2,
    )
    assert not result.ok, result.output
    written = repo / "big.bin"
    assert not written.exists() or written.stat().st_size <= 2 * 1024 * 1024


def test_core_dumps_are_always_disabled(repo: Path) -> None:
    result = run_python(repo, "import resource; print(resource.getrlimit(resource.RLIMIT_CORE))")
    assert result.ok, result.output
    assert "(0, 0)" in result.output


def test_process_ceiling_is_applied_when_asked(repo: Path) -> None:
    result = run_python(
        repo,
        "import resource; print(resource.getrlimit(resource.RLIMIT_NPROC))",
        processes=64,
    )
    assert result.ok, result.output
    assert "(64, 64)" in result.output


def test_normal_code_still_works_under_the_default_limits(repo: Path) -> None:
    """The defaults must be generous enough that ordinary work is unaffected."""
    config = Config.load(repo)
    result = RunPython().run(
        SkillContext(repo=repo, services={"config": config}),
        code="print(sum(range(1000000)))",
        timeout=30,
    )
    assert result.ok, result.output
    assert "499999500000" in result.output
    assert "MB address space" in result.data["limits"]


# -- the timeout kills the whole tree, not just the child ------------------


def test_the_child_runs_in_its_own_process_group(repo: Path) -> None:
    """start_new_session is what makes killing the tree possible."""
    result = run_python(repo, "import os; print(os.getpgid(0) == os.getpid())")
    assert result.ok, result.output
    assert "True" in result.output


def test_killing_the_group_takes_grandchildren_with_it(repo: Path) -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-s",
            "-c",
            "import os, time, sys\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    time.sleep(60); os._exit(0)\n"
            "print(pid, flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        grandchild = int(proc.stdout.readline().strip())
        os.kill(grandchild, 0)  # alive before the kill

        _kill_group(proc)
        os.waitpid(proc.pid, 0)

        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the grandchild outlived the kill of its process group")
    finally:
        if proc.poll() is None:
            _kill_group(proc)
            proc.wait(timeout=10)


def test_a_forked_grandchild_does_not_outlive_the_timeout(repo: Path) -> None:
    """The end-to-end version: the skill's own timeout reaps the tree."""
    marker = repo / "survivor.txt"
    code = (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(8)\n"
        "    open('survivor.txt', 'w').write('outlived the timeout')\n"
        "    os._exit(0)\n"
        "time.sleep(8)\n"
    )
    result = RunPython().run(
        SkillContext(repo=repo, services={"config": Config.load(repo)}),
        code=code,
        timeout=2,
    )
    assert result.data["timed_out"] is True
    time.sleep(9)
    assert not marker.exists(), "a grandchild wrote after the run was declared timed out"


# -- the helper ------------------------------------------------------------


def test_no_limits_means_no_preexec_hook() -> None:
    assert _limit_preexec(None) is None


def test_a_zeroed_limit_set_applies_nothing_but_core() -> None:
    hook = _limit_preexec(ResourceLimits(0, 0, 0, 0))
    assert hook is not None  # RLIMIT_CORE is always applied


def test_limits_are_read_from_the_config(repo: Path) -> None:
    (repo / "ourob.toml").write_text(
        (repo / "ourob.toml").read_text(encoding="utf-8")
        + "\n[policy.limits]\naddress_space_mb = 512\nfile_size_mb = 8\n",
        encoding="utf-8",
    )
    config = Config.load(repo)
    assert config.policy.limits.address_space_mb == 512
    assert config.policy.limits.file_size_mb == 8
    assert config.policy.limits.describe() == "512MB address space, 8MB per file"


def test_unknown_limit_keys_are_ignored(repo: Path) -> None:
    (repo / "ourob.toml").write_text(
        (repo / "ourob.toml").read_text(encoding="utf-8")
        + "\n[policy.limits]\nbogus_key = 1\n",
        encoding="utf-8",
    )
    assert Config.load(repo).policy.limits == ResourceLimits()


def test_run_command_is_limited_too(repo: Path) -> None:
    result = RunCommand().run(
        ctx_with(repo, address_space_mb=64),
        # no ';' -- the metacharacter filter forbids it, so build one expression
        argv=[
            "python3",
            "-c",
            "print(__import__('resource').getrlimit(__import__('resource').RLIMIT_AS))",
        ],
        timeout=30,
    )
    assert result.ok, result.output
    ceiling = 64 * 1024 * 1024
    assert str((ceiling, ceiling)) in result.output

