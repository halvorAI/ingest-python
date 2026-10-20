from __future__ import annotations

import time
import uuid


def new_event_id() -> str:
    """Prefixed id for events (ULID-shaped hex fallback without a ulid dep)."""
    # Compact time-sortable id: evt_ + 26 Crockford-ish chars from time+uuid.
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    ms = int(time.time() * 1000)
    chars: list[str] = []
    for _ in range(10):
        chars.append(alphabet[ms % 32])
        ms //= 32
    chars.reverse()
    rnd = uuid.uuid4().int
    for _ in range(16):
        chars.append(alphabet[rnd % 32])
        rnd //= 32
    return "evt_" + "".join(chars)


def now_utc() -> str:
    """RFC 3339 UTC with milliseconds."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
