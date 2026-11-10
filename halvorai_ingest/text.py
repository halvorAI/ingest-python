"""String lengths as APE counts them: in UTF-16 code units, like JavaScript's `length`."""

from __future__ import annotations


def utf16_len(value: str) -> int:
    """Length in UTF-16 code units. A character outside the BMP, such as most emoji, counts as 2."""
    return len(value) + sum(1 for ch in value if ord(ch) > 0xFFFF)


def cut(value: str, limit: int) -> str:
    """
    Cut `value` to at most `limit` UTF-16 code units, like JavaScript's `slice(0, limit)`, so the
    result fits the same APE limit the API and the Node.js and Go SDKs check. A character that
    would be split in half is dropped instead of leaving half a surrogate pair.
    """
    if len(value) <= limit // 2 or utf16_len(value) <= limit:
        return value
    units = 0
    for i, ch in enumerate(value):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return value[:i]
    return value
