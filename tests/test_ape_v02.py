"""APE v0.2 (halvorAI/halvorai-app#100, #109): validation and signing match the TypeScript SDK."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from halvorai_ingest import generate_source_key, verify_event
from halvorai_ingest.hash import normalise_instruction_text
from halvorai_ingest.sign import sign_event
from halvorai_ingest.validate import v02_fields_used, validate_event

V02 = Path(__file__).parent / "fixtures" / "ape-v0.2"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((V02 / name).read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("name", sorted(p.name for p in V02.glob("*.json")))
def test_the_published_v02_examples_are_valid(name: str) -> None:
    assert validate_event(load(name)) == []


def test_a_v01_event_cannot_use_a_v02_field() -> None:
    event = load("stablecoin-settled-payer-side.v0.2.json")
    event["ape_version"] = "0.1"
    assert v02_fields_used(event)
    assert validate_event(event)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["owner"].update(type="person"),  # unknown_reason with a known type
        lambda e: e["attribution_gaps"].append({"field": "agent", "reason": "not_provided"}),
        lambda e: e.update(delivery={"http_status": 99}),
    ],
)
def test_bad_v02_payment_events_are_refused(mutate: Any) -> None:
    event = load("x402-settled-payee-observed.v0.2.json")
    mutate(event)
    assert validate_event(event)


def test_a_report_needs_a_report_and_no_payment() -> None:
    report = load("agent-reported.v0.2.json")
    assert validate_event({k: v for k, v in report.items() if k != "report"})
    with_payment = {**report, "payment": load("stablecoin-settled-payer-side.v0.2.json")["payment"]}
    assert validate_event(with_payment)


def test_the_signature_header_names_the_event_version() -> None:
    key = generate_source_key()
    for name, version in (("agent-reported.v0.2.json", "0.2"),):
        event = load(name)
        event["source"]["id"] = key["kid"]
        sig = sign_event(event, key)
        header = json.loads(_b64(sig.split(".")[0]))
        assert header["ape"] == version
        assert verify_event(event, sig, key)
        assert not verify_event({**event, "ape_version": "0.1"}, sig, key)


def test_ws_lower_v1_matches_the_typescript_sdk() -> None:
    a = "Buy  the Tidewater export from http://localhost:8123/pay?item=1 now"
    b = "buy the tidewater export from http://127.0.0.1:9000/pay?item=1   now"
    assert normalise_instruction_text(a) == normalise_instruction_text(b)
    assert normalise_instruction_text(a) == "buy the tidewater export from /pay?item=1 now"
    assert normalise_instruction_text("Café") == normalise_instruction_text("Café")


def _b64(segment: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
