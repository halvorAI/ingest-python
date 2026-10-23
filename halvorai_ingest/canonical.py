"""RFC 8785 JSON Canonicalization Scheme (JCS).

Numbers are serialized by the `rfc8785` package (ECMAScript number formatting). Strings and
objects are serialized here so the output matches the Node.js SDK byte for byte, including
strings that hold a lone UTF-16 surrogate: like `JSON.stringify` in ES2019+, those are written
as a lowercase `\\udXXX` escape instead of being rejected.
"""

from __future__ import annotations

import math
from typing import Any

import rfc8785

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _string(s: str) -> str:
    out = ['"']
    for ch in s:
        code = ord(ch)
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif code < 0x20 or 0xD800 <= code <= 0xDFFF:
            # Control characters, and surrogates Python holds unpaired (a valid pair is one code
            # point in a Python str, so anything in this range here is a lone surrogate).
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(key: str) -> list[int]:
    """Sort key for object members: UTF-16 code units, as RFC 8785 §3.2.3 requires."""
    units: list[int] = []
    for ch in key:
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            units.append(0xD800 + (code >> 10))
            units.append(0xDC00 + (code & 0x3FF))
        else:
            units.append(code)
    return units


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise rfc8785.FloatDomainError(value)
        out.append(rfc8785.dumps(value).decode("ascii"))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise rfc8785.CanonicalizationError(f"object keys must be strings, got {type(key).__name__}")
        out.append("{")
        for i, key in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")
    else:
        raise rfc8785.CanonicalizationError(f"unsupported type: {type(value).__name__}")


def canonicalize(value: Any) -> str:
    """Return the JCS string for a JSON value."""
    out: list[str] = []
    _serialize(value, out)
    return "".join(out)
