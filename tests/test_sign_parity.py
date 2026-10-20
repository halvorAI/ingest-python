"""P1.3 / P3.7: signature vector verifies identically in Python."""

from __future__ import annotations

import json
from pathlib import Path

from halvorai_ingest.canonical import canonicalize
from halvorai_ingest.sign import sign_event, verify_event

VECTOR = Path(__file__).resolve().parent / "fixtures" / "sign-vector.json"


def test_sign_vector_file_exists() -> None:
    assert VECTOR.is_file(), f"missing {VECTOR}"


def test_canonical_jcs_matches_vector() -> None:
    data = json.loads(VECTOR.read_text(encoding="utf-8"))
    assert canonicalize(data["event"]) == data["canonical_jcs"]


def test_verify_committed_signature() -> None:
    data = json.loads(VECTOR.read_text(encoding="utf-8"))
    assert verify_event(data["event"], data["sig"], data["public_jwk"]) is True


def test_python_resign_matches_vector() -> None:
    data = json.loads(VECTOR.read_text(encoding="utf-8"))
    sig = sign_event(data["event"], data["private_jwk"])
    assert sig == data["sig"]
    assert verify_event(data["event"], sig, data["public_jwk"]) is True


def test_tamper_fails() -> None:
    data = json.loads(VECTOR.read_text(encoding="utf-8"))
    event = dict(data["event"])
    event["id"] = event["id"] + "x"
    assert verify_event(event, data["sig"], data["public_jwk"]) is False
