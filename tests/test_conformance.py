"""ape-schema conformance data: the same verdicts and bytes as every other APE implementation.

The files in tests/fixtures/conformance are copied from
https://github.com/halvorAI/ape-schema/tree/main/conformance. Refresh them when the schema changes.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from halvorai_ingest.canonical import canonicalize
from halvorai_ingest.validate import validate_event

HERE = Path(__file__).resolve().parent / "fixtures" / "conformance"
CASES: list[dict[str, Any]] = json.loads((HERE / "events.json").read_text(encoding="utf-8"))["cases"]
JCS: dict[str, Any] = json.loads((HERE / "jcs-vectors.json").read_text(encoding="utf-8"))


def verdict(event: dict[str, Any]) -> str:
    problems = validate_event(event)
    if not problems:
        return "valid"
    return "pan" if any("pan_detected" in p for p in problems) else "schema"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_events(case: dict[str, Any]) -> None:
    expected = "valid" if case["expect"] == "valid" else case["rule"]
    assert verdict(case["event"]) == expected


@pytest.mark.parametrize("vector", JCS["numbers"], ids=[v["bits"] for v in JCS["numbers"]])
def test_jcs_numbers(vector: dict[str, str]) -> None:
    (number,) = struct.unpack(">d", bytes.fromhex(vector["bits"]))
    assert canonicalize(number) == vector["expected"]


@pytest.mark.parametrize("vector", JCS["objects"])
def test_jcs_objects(vector: dict[str, str]) -> None:
    assert canonicalize(json.loads(vector["json"])) == vector["expected"]
