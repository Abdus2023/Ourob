"""Repository configuration (``ourob.toml``).

The config is deliberately small and deliberately *protected*: it is where the
runtime is told which paths it may not touch without a ratified amendment and
which gates must be green before anything is promoted.  A system that can edit
its own guardrails has no guardrails, so editing this file is a constitutional
act, not an engineering one.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_PROTECTED = (
    "ourob.toml",
    "bootstrap.py",
    "bootstrap.lock.json",
    "src/ourob/bootstrap/",
    "src/ourob/policies/",
    "src/ourob/verify/",
)


@dataclass
class VerifyConfig:
    gates: list[str] = field(default_factory=lambda: [
        "policy-integrity",
        "skill-contract",
        "compile",
        "import",
        "manifest",
        "lint",
        "tests",
    ])
    timeout: int = 600
    blocking_gates: list[str] = field(default_factory=lambda: ["lint"])


@dataclass
class PolicyConfig:
    protected: list[str] = field(default_factory=lambda: list(DEFAULT_PROTECTED))
    max_steps: int = 32
    max_file_bytes: int = 1_000_000
    allow_commands: list[str] = field(
        default_factory=lambda: [
            "python",
            "python3",
            "pytest",
            "git status",
            "git diff",
            "git log",
            "git add",
            "git commit",
            "ruff",
            "mypy",
        ]
    )
    deny_patterns: list[str] = field(
        default_factory=lambda: [
            "rm -rf",
            "sudo ",
            "curl",
            "wget",
            "nc ",
            "ssh ",
            ":(){",
            "chmod -R",
            "chown ",
            "> /dev/",
        ]
    )


@dataclass
class Config:
    repo: Path
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    raw: dict[str, Any] = field(default_factory=dict)
    source: str = "defaults"

    @classmethod
    def load(cls, repo: Path) -> Config:
        repo = Path(repo).resolve()
        path = repo / "ourob.toml"
        config = cls(repo=repo)
        if not path.is_file():
            config.source = "defaults"
            return config
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        config.raw = data
        config.source = path.as_posix()

        verify = data.get("verify", {})
        if "gates" in verify:
            config.verify.gates = [str(g) for g in verify["gates"]]
        if "timeout" in verify:
            config.verify.timeout = int(verify["timeout"])
        config.verify.blocking_gates = [str(g) for g in verify.get("blocking_gates", [])]

        policy = data.get("policy", {})
        if "protected" in policy:
            config.policy.protected = [str(p) for p in policy["protected"]]
        if not config.policy.protected:
            raise ConfigError("[policy].protected must not be empty")
        for key in ("max_steps", "max_file_bytes"):
            if key in policy:
                setattr(config.policy, key, int(policy[key]))
        if "allow_commands" in policy:
            config.policy.allow_commands = [str(c) for c in policy["allow_commands"]]
        if "deny_patterns" in policy:
            config.policy.deny_patterns = [str(p) for p in policy["deny_patterns"]]
        return config

    def is_protected(self, relpath: str) -> bool:
        normalised = relpath.replace("\\", "/").lstrip("./")
        for pattern in self.policy.protected:
            pattern = pattern.replace("\\", "/").lstrip("./")
            if pattern.endswith("/"):
                if normalised.startswith(pattern) or normalised + "/" == pattern:
                    return True
            elif normalised == pattern:
                return True
        return False

    def protected_prefixes(self) -> list[str]:
        return list(self.policy.protected)
