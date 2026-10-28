"""Redact PANs, CVCs, secret keys and private keys from free text."""

from __future__ import annotations

import re
from typing import Any, Mapping, Pattern

SECRET_KEY = re.compile(
    r"\b(?:sk_live_|sk_test_|rk_live_|whsec_|hk_live_|hk_test_)[0-9A-Za-z]+\b"
)
PEM_PRIVATE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
JWK_D = re.compile(r'"d"\s*:\s*"[^"]*"')
CVC = re.compile(
    r"\b(cvc|cvv2?|security code)(\"\s*:\s*\"|\s*[:=]\s*|\s+)(\d{3,4})(\")?",
    re.IGNORECASE,
)
# Digits glued to letters or more digits (hex signatures, base64, ids) are not a card number.
PAN_RUN = re.compile(r"(?<![0-9A-Za-z])\d(?:[ -]?\d)*(?![0-9A-Za-z])")

MAX_DEPTH = 64


def _luhn_valid(digits: str) -> bool:
    total = 0
    double = False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def _redact_pan(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        run = m.group(0)
        digits = re.sub(r"[ -]", "", run)
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return "[redacted:pan]"
        return run

    return PAN_RUN.sub(repl, text)


def redact_text(text: str, extra_patterns: list[Pattern[str] | tuple[Pattern[str], str]] | None = None) -> str:
    out = PEM_PRIVATE.sub("[redacted:private-key]", text)
    out = SECRET_KEY.sub("[redacted:secret-key]", out)
    out = JWK_D.sub('"d":"[redacted:private-key]"', out)
    out = _redact_pan(out)

    def cvc_repl(m: re.Match[str]) -> str:
        return f"{m.group(1)}{m.group(2)}[redacted:cvc]{m.group(4) or ''}"

    out = CVC.sub(cvc_repl, out)
    if extra_patterns:
        for item in extra_patterns:
            if isinstance(item, tuple):
                pattern, label = item
                replacement = f"[redacted:{label}]" if label else "[redacted]"
                out = pattern.sub(replacement, out)
            else:
                out = item.sub("[redacted]", out)
    return out


def redact_deep(value: Any, extra_patterns: list[Pattern[str] | tuple[Pattern[str], str]] | None = None, *, _depth: int = 0) -> Any:
    if _depth > MAX_DEPTH:
        return None
    if isinstance(value, str):
        return redact_text(value, extra_patterns)
    if isinstance(value, list):
        return [redact_deep(v, extra_patterns, _depth=_depth + 1) for v in value]
    if isinstance(value, Mapping):
        if isinstance(value.get("kty"), str) and isinstance(value.get("d"), str):
            out = dict(value)
            out["d"] = "[redacted:private-key]"
            return out
        return {k: redact_deep(v, extra_patterns, _depth=_depth + 1) for k, v in value.items()}
    return value
