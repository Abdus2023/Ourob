"""The bootstrap mechanism: manifest, amendments, snapshots, cold start, promotion."""

from .amend import Amendment, AmendmentLedger, AmendmentStatus
from .coldstart import BootReport, boot, ensure_importable, locate_source_root
from .manifest import LOCK_NAME, FileRecord, Manifest, ManifestDiff, compare
from .promote import Promotion, PromotionResult
from .snapshot import Snapshot

__all__ = [
    "LOCK_NAME",
    "Amendment",
    "AmendmentLedger",
    "AmendmentStatus",
    "BootReport",
    "FileRecord",
    "Manifest",
    "ManifestDiff",
    "Promotion",
    "PromotionResult",
    "Snapshot",
    "boot",
    "compare",
    "ensure_importable",
    "locate_source_root",
]
