from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    # Keep UTC semantics without using deprecated datetime.utcnow().
    return datetime.now(UTC).replace(tzinfo=None)
