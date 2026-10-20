from __future__ import annotations

import base64
import json
from typing import Any


def decode_payment_header(value: str) -> Any:
    """Decode a base64url-encoded JSON payment header (x402 v2 HTTP transport)."""
    pad = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value.replace("-", "+").replace("_", "/") + pad)
    return json.loads(raw.decode("utf-8"))


def encode_payment_header(value: Any) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
