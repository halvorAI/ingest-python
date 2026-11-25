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


def normalise_instruction_text(text: str) -> str:
    """`ws-lower-v1` (APE v0.2): NFC, lowercase, URL scheme, host and port removed (path kept),
    whitespace collapsed to one space, trimmed. Matches the TypeScript SDK."""
    import re
    import unicodedata

    out = unicodedata.normalize("NFC", text).lower()
    out = re.sub(r"[a-z][a-z0-9+.-]*://[^\s/?#]+", "", out)
    return re.sub(r"\s+", " ", out).strip()
