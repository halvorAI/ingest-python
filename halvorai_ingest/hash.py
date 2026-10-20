from __future__ import annotations

import hashlib


def hash_instruction(text: str) -> str:
    """APE instruction digest: `sha256:` + hex of the UTF-8 instruction text."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def evidence_sha256(value: str | object) -> str:
    """Digest of an evidence value (UTF-8 for strings, JCS for objects)."""
    from .canonical import canonicalize

    raw = value if isinstance(value, str) else canonicalize(value)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
