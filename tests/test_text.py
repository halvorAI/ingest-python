from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from halvorai_ingest.text import cut, utf16_len
from halvorai_ingest.validate import validate_event


def test_utf16_len_counts_astral_characters_twice() -> None:
    assert utf16_len("abc") == 3
    assert utf16_len("é") == 1
    assert utf16_len("😀") == 2
    assert utf16_len("a😀b") == 4


def test_cut_matches_javascript_slice_without_splitting_pairs() -> None:
    assert cut("abc", 5) == "abc"
    assert cut("abcdef", 3) == "abc"
    assert cut("😀" * 100, 128) == "😀" * 64
    assert cut("a" * 127 + "😀", 128) == "a" * 127
    assert utf16_len(cut("x😀" * 300, 256)) <= 256


def _valid_event() -> dict[str, Any]:
    cases = json.loads((Path(__file__).resolve().parent / "fixtures" / "conformance" / "events.json").read_text("utf-8"))
    event: dict[str, Any] = next(c["event"] for c in cases["cases"] if c["expect"] == "valid")
    return event


def test_validation_counts_utf16_units() -> None:
    event = _valid_event()
    event["agent"]["name"] = "😀" * 64  # 128 UTF-16 units: at the limit
    assert validate_event(event) == []
    event["agent"]["name"] = "😀" * 65  # 65 characters but 130 UTF-16 units: over the 128 limit
    assert validate_event(event) != []
