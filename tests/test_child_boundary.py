"""Adversarial checks for the pre-exec filesystem boundary.

These tests exercise the OS boundary directly rather than relying only on a
policy denial or a later manifest mismatch. They attempt content/namespace,
symlink, traversal, hard-link, rename, and replace operations from a real Python
child, and explicitly record that file-mode and timestamp changes remain allowed.
"""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
from pathlib import Path

import pytest

from ourob.child_sandbox import landlock_status, run_child
from ourob.config import Config


def test_child_protected_content_boundary_and_metadata_gaps(repo: Path) -> None:
    status = landlock_status()
    if not status.available:
        pytest.skip(f"host cannot provide the required boundary: {status.reason}")

    config = Config.load(repo)
    protected = list(config.policy.protected)
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    original = target.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    original_stat = target.stat()
    original_mode = original_stat.st_mode
    original_atime_ns = original_stat.st_atime_ns
    original_mtime_ns = original_stat.st_mtime_ns

    docs = repo / "docs"
    file_alias = docs / "protected-file-link.py"
    dir_alias = docs / "protected-dir-link"
    file_alias.symlink_to(os.path.relpath(target, docs))
    dir_alias.symlink_to(repo / "src" / "ourob" / "policies", target_is_directory=True)
    (docs / "replace-source.py").write_text("replacement\n", encoding="utf-8")

    script = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path

        target = Path("src/ourob/policies/rules.py")
        outcomes = {}

        def attempt(name, operation):
            try:
                operation()
            except OSError as exc:
                outcomes[name] = f"denied:{exc.errno}"
            else:
                outcomes[name] = "allowed"

        attempt("relative-overwrite", lambda: target.write_text("tampered\\n"))
        attempt("absolute-overwrite", lambda: Path(target.resolve()).write_text("tampered\\n"))
        attempt(
            "traversal-overwrite",
            lambda: Path("docs/../src/ourob/policies/rules.py").write_text("tampered\\n"),
        )
        attempt("unlink", lambda: target.unlink())
        attempt("mkdir-under-protected-dir", lambda: Path("src/ourob/policies/child-created").mkdir())
        attempt(
            "symlink-file-overwrite",
            lambda: Path("docs/protected-file-link.py").write_text("tampered\\n"),
        )
        attempt(
            "symlink-dir-create",
            lambda: Path("docs/protected-dir-link/child.py").write_text("tampered\\n"),
        )
        attempt("replace-into-protected", lambda: os.replace("docs/replace-source.py", target))
        attempt("rename-out-of-protected", lambda: os.rename(target, "docs/moved-rules.py"))
        attempt("hardlink-from-protected", lambda: os.link(target, "docs/hardlinked-rules.py"))
        attempt("chmod", lambda: os.chmod(target, 0o600))
        attempt(
            "utime",
            lambda: os.utime(target, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000)),
        )
        attempt("ordinary-write-control", lambda: Path("docs/child-write-ok.txt").write_text("ok\\n"))
        print(json.dumps(outcomes, sort_keys=True))
        """
    )
    rc, stdout, stderr = run_child(
        [os.fspath(Path(os.sys.executable)), "-c", script],
        repo=repo,
        protected=protected,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.fspath(Path.home()),
            "PYTHONNOUSERSITE": "1",
        },
        timeout=30,
    )
    assert rc == 0, f"child exited {rc}: {stderr}\n{stdout}"
    outcomes = json.loads(stdout.splitlines()[-1])
    for name in (
        "relative-overwrite",
        "absolute-overwrite",
        "traversal-overwrite",
        "unlink",
        "mkdir-under-protected-dir",
        "symlink-file-overwrite",
        "symlink-dir-create",
        "replace-into-protected",
        "rename-out-of-protected",
        "hardlink-from-protected",
    ):
        assert outcomes[name].startswith("denied:"), f"{name} unexpectedly {outcomes[name]}"
    # Landlock does not mediate chmod or utime. Keep these adversarial
    # observations as explicit limitations rather than claiming metadata protection.
    assert outcomes["chmod"] == "allowed"
    assert outcomes["utime"] == "allowed"
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_mtime_ns == 1_600_000_000_000_000_000
    target.chmod(original_mode & 0o777)
    os.utime(target, ns=(original_atime_ns, original_mtime_ns))
    assert outcomes["ordinary-write-control"] == "allowed"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == original_hash
    assert target.stat().st_mode == original_mode
    assert (docs / "child-write-ok.txt").read_text(encoding="utf-8") == "ok\n"
    assert not (repo / "src" / "ourob" / "policies" / "child-created").exists()


def test_python_child_skill_uses_the_same_boundary(repo: Path) -> None:
    from ourob.skills.base import SkillContext
    from ourob.skills.registry import SkillRegistry
    from ourob.state.model import Invocation

    registry = SkillRegistry(repo)
    registry.discover()
    target = repo / "src" / "ourob" / "policies" / "rules.py"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    result = registry.dispatch(
        Invocation(
            skill="run_python",
            args={
                "code": (
                    "from pathlib import Path; Path('src/ourob/policies/rules.py').write_text('tampered')"
                )
            },
        ),
        SkillContext(repo=repo, services={"config": Config.load(repo)}),
    )
    assert not result.ok
    assert "PermissionError" in result.output or result.data.get("exit_code") != 0
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before


def test_kernel_journals_prevented_python_child_write_and_control(repo: Path) -> None:
    from ourob.kernel import Kernel
    from ourob.planner.scripted import Plan, ScriptedPlanner
    from ourob.state.store import StateStore

    status = landlock_status()
    if not status.available:
        pytest.skip(f"host cannot provide the required boundary: {status.reason}")

    target = repo / "src" / "ourob" / "policies" / "rules.py"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    code = textwrap.dedent(
        """
        import json
        from pathlib import Path

        from ourob.fsx import atomic_write

        target = Path("src/ourob/policies/rules.py")
        try:
            target.write_text("tampered\\n")
        except OSError as exc:
            protected = f"denied:{exc.errno}"
        else:
            protected = "allowed"
        Path("docs/child-boundary-control.txt").write_text("ordinary write allowed\\n")
        atomic_write(Path("docs/child-atomic-write-control.txt"), "atomic write allowed\\n")
        print(json.dumps({"protected": protected}, sort_keys=True))
        """
    )
    plan = Plan.from_dict(
        {
            "goal": "attempt a protected write in a Python child and record the boundary",
            "max_steps": 3,
            "steps": [
                {"skill": "run_python", "args": {"code": code, "timeout": 30}},
                {"skill": "finish", "args": {"summary": "child boundary checked", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    child_step = outcome.run.steps[0]
    assert child_step.result is not None and child_step.result.ok
    assert child_step.result.data["exit_code"] == 0
    assert json.loads(child_step.result.output.splitlines()[-1]) == {"protected": "denied:13"}
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    assert (repo / "docs" / "child-boundary-control.txt").read_text(
        encoding="utf-8"
    ) == "ordinary write allowed\n"
    assert (repo / "docs" / "child-atomic-write-control.txt").read_text(
        encoding="utf-8"
    ) == "atomic write allowed\n"

    events = list(StateStore(repo).journal(outcome.run.run_id).events())
    decisions = [
        event["payload"]["decisions"]
        for event in events
        if event["kind"] == "policy.decision" and event["payload"].get("index") == 0
    ]
    assert len(decisions) == 1
    child_boundary = next(item for item in decisions[0] if item["policy"] == "child-filesystem")
    assert child_boundary["allowed"]
    result_event = next(
        event for event in events if event["kind"] == "skill.result" and event["payload"].get("index") == 0
    )
    assert result_event["payload"]["result"]["data"]["filesystem_boundary"].startswith("Landlock")
    assert all(item["ok"] for item in StateStore(repo).check_chain())


def test_result001_python_c_child_cannot_change_protected_file_or_journal(repo: Path) -> None:
    from ourob.kernel import Kernel
    from ourob.planner.scripted import Plan, ScriptedPlanner
    from ourob.state.store import StateStore

    status = landlock_status()
    if not status.available:
        pytest.skip(f"host cannot provide the required boundary: {status.reason}")

    target = repo / "src" / "ourob" / "policies" / "rules.py"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    plan = Plan.from_dict(
        {
            "goal": "replay the Result 001 allowlisted python -c mutation attempt",
            "max_steps": 4,
            "steps": [
                {
                    "skill": "run_command",
                    "args": {
                        "argv": [
                            "python",
                            "-c",
                            "open('src/ourob/policies/rules.py', 'w').write('tampered')",
                        ]
                    },
                },
                {
                    "skill": "run_command",
                    "args": {
                        "argv": [
                            "python",
                            "-c",
                            "open('.ourob/system.jsonl', 'w').write('forged')",
                        ]
                    },
                },
                {
                    "skill": "finish",
                    "args": {"summary": "the historical child writes were blocked", "success": True},
                },
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    assert outcome.failed == 2
    assert all(not item.result.ok for item in outcome.run.steps[:2] if item.result is not None)
    assert all(
        "PermissionError" in (item.result.output if item.result else "") for item in outcome.run.steps[:2]
    )
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    assert not (repo / ".ourob" / "system.jsonl").exists()

    store = StateStore(repo)
    assert all(item["ok"] for item in store.check_chain())
    events = list(store.journal(outcome.run.run_id).events())
    decisions = [event for event in events if event["kind"] == "policy.decision"]
    assert len(decisions) == 3
    for event in decisions[:2]:
        policies = event["payload"]["decisions"]
        assert next(item for item in policies if item["policy"] == "command-allowlist")["allowed"]
        assert next(item for item in policies if item["policy"] == "child-filesystem")["allowed"]
    results = [
        event
        for event in events
        if event["kind"] == "skill.result" and event["payload"].get("index") in {0, 1}
    ]
    assert len(results) == 2
    assert all(event["payload"]["result"]["data"]["exit_code"] != 0 for event in results)


def test_command_skill_cannot_replace_protected_file(repo: Path) -> None:
    from ourob.kernel import Kernel
    from ourob.planner.scripted import Plan, ScriptedPlanner
    from ourob.state.store import StateStore

    status = landlock_status()
    if not status.available:
        pytest.skip(f"host cannot provide the required boundary: {status.reason}")

    target = repo / "src" / "ourob" / "policies" / "rules.py"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    script = repo / "docs" / "child-command-attack.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            from pathlib import Path
            target = Path("src/ourob/policies/rules.py")
            try:
                target.write_text("replacement\\n")
            except OSError as exc:
                outcome = f"denied:{exc.errno}"
            else:
                outcome = "allowed"
            print(json.dumps({"replace": outcome}))
            """
        ),
        encoding="utf-8",
    )
    plan = Plan.from_dict(
        {
            "goal": "try to replace a protected file through the command skill",
            "max_steps": 3,
            "steps": [
                {"skill": "run_command", "args": {"argv": ["python", "docs/child-command-attack.py"]}},
                {"skill": "finish", "args": {"summary": "replacement was denied", "success": True}},
            ],
        }
    )
    outcome = Kernel(repo, verify_at_end=False).run(plan.goal, ScriptedPlanner(plan))
    child_step = outcome.run.steps[0]
    assert child_step.result is not None and child_step.result.ok
    assert json.loads(child_step.result.output.splitlines()[-1]) == {"replace": "denied:13"}
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before

    events = list(StateStore(repo).journal(outcome.run.run_id).events())
    decisions = next(
        event["payload"]["decisions"]
        for event in events
        if event["kind"] == "policy.decision" and event["payload"].get("index") == 0
    )
    assert next(item for item in decisions if item["policy"] == "command-allowlist")["allowed"]
    assert next(item for item in decisions if item["policy"] == "child-filesystem")["allowed"]
    assert all(item["ok"] for item in StateStore(repo).check_chain())


@pytest.mark.parametrize("reserved_root", [".ourob", ".git"])
def test_reserved_root_symlink_cannot_borrow_an_unprotected_write_rule(
    repo: Path, reserved_root: str
) -> None:
    status = landlock_status()
    if not status.available:
        pytest.skip(f"host cannot provide the required boundary: {status.reason}")

    docs = repo / "docs"
    alias = repo / reserved_root
    alias.symlink_to(docs, target_is_directory=True)
    marker = docs / "protected.json"
    script = textwrap.dedent(
        f"""
        import json
        from pathlib import Path
        target = Path({(reserved_root + "/protected.json")!r})
        try:
            target.write_text("escape\\n")
        except OSError as exc:
            result = f"denied:{{exc.errno}}"
        else:
            result = "allowed"
        print(json.dumps({{"result": result}}))
        """
    )
    rc, stdout, stderr = run_child(
        [os.fspath(Path(os.sys.executable)), "-c", script],
        repo=repo,
        protected=list(Config.load(repo).policy.protected),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONNOUSERSITE": "1"},
        timeout=30,
    )
    assert rc == 0, f"child exited {rc}: {stderr}\\n{stdout}"
    assert json.loads(stdout.splitlines()[-1]) == {"result": "denied:13"}
    assert not marker.exists()


def test_unavailable_child_boundary_fails_closed(repo: Path, monkeypatch) -> None:
    from ourob.child_sandbox import LandlockStatus

    monkeypatch.setattr(
        "ourob.child_sandbox.landlock_status",
        lambda: LandlockStatus(False, reason="test unavailable"),
    )
    rc, out, err = run_child(
        [os.sys.executable, "-c", "print('must not run')"],
        repo=repo,
        protected=Config.load(repo).policy.protected,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        timeout=10,
    )
    assert rc == 126
    assert not out
    assert "child execution refused" in err
