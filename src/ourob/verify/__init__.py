"""Verification machinery: gates, the suite, and report formatting."""

from .gates import (
    GATE_CLASSES,
    BootstrapGate,
    CompileGate,
    Gate,
    GateContext,
    ImportGate,
    LintGate,
    ManifestGate,
    PolicyIntegrityGate,
    SkillContractGate,
    TestGate,
    build_gates,
    unknown_gates,
)
from .suite import (
    SuiteOutcome,
    VerificationSuite,
    format_report,
    report_to_dict,
    verify_repo,
)

__all__ = [
    "GATE_CLASSES",
    "BootstrapGate",
    "CompileGate",
    "Gate",
    "GateContext",
    "ImportGate",
    "LintGate",
    "ManifestGate",
    "PolicyIntegrityGate",
    "SkillContractGate",
    "SuiteOutcome",
    "TestGate",
    "VerificationSuite",
    "build_gates",
    "format_report",
    "report_to_dict",
    "unknown_gates",
    "verify_repo",
]
