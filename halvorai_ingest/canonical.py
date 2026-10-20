"""RFC 8785 JSON Canonicalization Scheme via the `rfc8785` package."""

from __future__ import annotations

from typing import Any

import rfc8785


def canonicalize(value: Any) -> str:
    """Return the JCS string for a JSON value (UTF-8 decoded)."""
    return rfc8785.dumps(value).decode("utf-8")
