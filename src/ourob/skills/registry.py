"""Skill discovery and dispatch.

Discovery has two tiers: builtins shipped in the package, and contrib modules
loaded from ``src/ourob/skills/contrib`` when a registry is created. A registry
is an immutable startup snapshot for the lifetime of its runtime. Files added,
changed, or removed during a run are reported as pending and are not imported or
invoked by that process; verify and promote them, then start a fresh runtime.

``discover()`` is idempotent and returns the problems it found rather than
raising, because a half-written contrib skill must not stop the runtime from
booting -- it should stop the runtime from *using* that skill.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path
from typing import Any

from ..errors import SkillError, SkillNotFound
from ..schema import coerce
from ..state.model import Invocation, SkillResult
from .base import SKILL_ATTR, Skill, SkillContext

BUILTIN_PACKAGE = "ourob.skills.builtin"
CONTRIB_DIR = "src/ourob/skills/contrib"


class SkillRegistry:
    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo).resolve()
        self._skills: dict[str, Skill] = {}
        self._modules: dict[str, str] = {}
        self._problems: list[str] = []
        self._contrib_snapshot: dict[str, str] | None = None
        self._discovered = False
        self._frozen = False

    # -- discovery --------------------------------------------------------
    def discover(self) -> list[str]:
        """Take the one immutable discovery snapshot for this registry."""
        if self._discovered:
            return list(self._problems)
        self._skills.clear()
        self._modules.clear()
        self._problems = []
        self._load_builtin()
        self._load_contrib()
        self._contrib_snapshot = self._scan_contrib()
        self._discovered = True
        return list(self._problems)

    def _load_builtin(self) -> None:
        try:
            package = importlib.import_module(BUILTIN_PACKAGE)
        except Exception as exc:
            self._problems.append(f"{BUILTIN_PACKAGE}: import failed: {exc}")
            return
        for module_info in pkgutil.iter_modules(package.__path__):
            if module_info.name.startswith("_"):
                continue
            qualified = f"{BUILTIN_PACKAGE}.{module_info.name}"
            try:
                module = importlib.import_module(qualified)
            except Exception as exc:
                self._problems.append(f"{qualified}: import failed: {exc}")
                continue
            self._harvest(module, qualified)

    def _load_contrib(self) -> None:
        contrib = self.repo / CONTRIB_DIR
        if contrib.is_symlink():
            self._problems.append(f"{CONTRIB_DIR}: contrib directory symlink is not followed")
            return
        if not contrib.is_dir():
            return
        for path in sorted(contrib.glob("*.py")):
            if path.name.startswith("_"):
                continue
            relpath = path.relative_to(self.repo).as_posix()
            if path.is_symlink():
                self._problems.append(f"{relpath}: contrib symlink is not followed")
                continue
            module_name = f"ourob_contrib_{path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                self._problems.append(f"{relpath}: cannot build an import spec")
                continue
            module = importlib.util.module_from_spec(spec)
            # The @skill decorator resolves its class's module through
            # sys.modules, so the module has to be registered before it runs.
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                sys.modules.pop(module_name, None)
                self._problems.append(f"{relpath}: import failed: {type(exc).__name__}: {exc}")
                continue
            self._harvest(module, relpath)

    def _scan_contrib(self) -> dict[str, str]:
        """Fingerprint contrib files without importing or executing them."""
        contrib = self.repo / CONTRIB_DIR
        if contrib.is_symlink() or not contrib.is_dir():
            return {}
        snapshot: dict[str, str] = {}
        for path in sorted(contrib.glob("*.py")):
            if path.name.startswith("_"):
                continue
            relpath = path.relative_to(self.repo).as_posix()
            try:
                snapshot[relpath] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                snapshot[relpath] = f"unreadable:{type(exc).__name__}:{exc}"
        return snapshot

    def discovery_status(self) -> dict[str, Any]:
        """Compare the startup registry snapshot with current contrib bytes."""
        if self._contrib_snapshot is None:
            return {
                "snapshot": "not-discovered",
                "complete": False,
                "restart_required": True,
                "pending": {"added": [], "changed": [], "removed": []},
            }
        current = self._scan_contrib()
        baseline = self._contrib_snapshot
        added = sorted(set(current) - set(baseline))
        removed = sorted(set(baseline) - set(current))
        changed = sorted(path for path in set(current) & set(baseline) if current[path] != baseline[path])
        pending = {"added": added, "changed": changed, "removed": removed}
        stale = bool(added or changed or removed)
        return {
            "snapshot": "runtime-start",
            "complete": not stale,
            "restart_required": stale,
            "pending": pending,
        }

    def _harvest(self, module: Any, origin: str) -> None:
        for cls in getattr(module, SKILL_ATTR, []):
            spec = getattr(cls, "spec", None)
            if spec is None:
                self._problems.append(f"{origin}: {cls.__name__} has no skill spec")
                continue
            if spec.name in self._skills:
                self._problems.append(
                    f"{origin}: duplicate skill name {spec.name!r} "
                    f"(already provided by {self._modules.get(spec.name)})"
                )
                continue
            try:
                self._skills[spec.name] = cls()
            except Exception as exc:
                self._problems.append(f"{origin}: cannot instantiate {cls.__name__}: {exc}")
                continue
            self._modules[spec.name] = origin

    # -- access -----------------------------------------------------------
    def freeze(self) -> None:
        """Prevent registry mutation once it is bound to a running Kernel."""
        if not self._discovered:
            raise SkillError("cannot freeze the skill registry before discovery")
        self._frozen = True

    def register(self, cls: type[Skill]) -> None:
        if self._frozen:
            raise SkillError("skill registry is frozen; new skills require a fresh runtime")
        spec = getattr(cls, "spec", None)
        if spec is None:
            raise SkillError(f"{cls.__name__} is missing a skill spec; use @skill(...)")
        self._skills[spec.name] = cls()
        self._modules[spec.name] = cls.__module__

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise SkillNotFound(
                f"no skill named {name!r}; available: {', '.join(sorted(self._skills)) or '(none)'}"
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._skills

    def names(self) -> list[str]:
        return sorted(self._skills)

    def catalogue(self) -> list[dict[str, Any]]:
        out = []
        for name in self.names():
            entry = self._skills[name].spec.to_dict()
            entry["module"] = self._modules.get(name, "")
            out.append(entry)
        return out

    def problems(self) -> list[str]:
        return list(self._problems)

    # -- dispatch ---------------------------------------------------------
    def dispatch(self, invocation: Invocation, ctx: SkillContext) -> SkillResult:
        """Validate arguments against the declared schema, then execute."""
        try:
            instance = self.get(invocation.skill)
        except SkillNotFound as exc:
            message = str(exc)
            status = self.discovery_status()
            if status["restart_required"]:
                pending = [path for paths in status["pending"].values() for path in paths]
                message += (
                    "; contrib discovery snapshot is stale; verify/promote and start a fresh "
                    f"runtime before invoking pending files: {', '.join(pending)}"
                )
            return SkillResult(ok=False, error=message, output=message)
        try:
            args = coerce(invocation.args, instance.spec.params)
        except SkillError as exc:
            return SkillResult(
                ok=False,
                error=f"invalid arguments: {exc}",
                output=f"invalid arguments for {invocation.skill}: {exc}",
            )
        from ..bootstrap.amend import skill_execution

        with skill_execution():
            return instance.timed(ctx, **args)
