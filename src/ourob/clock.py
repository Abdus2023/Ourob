"""Time and identity primitives, shared by the state model and the bootstrap."""

from __future__ import annotations

import time
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def now() -> float:
    return time.time()


def stamp(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))
