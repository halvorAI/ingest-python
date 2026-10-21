"""Validation before signing, environment-aware endpoints and per-event delivery results."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from halvorai_ingest import HalvorClient, agent_context, generate_source_key
from halvorai_ingest.client import events_url
from halvorai_ingest.validate import validate_event

PAYMENT = {
    "payment_id": "pi_3QxAbCdEfGhIjKlM",
    "rail": "stripe",
    "amount": {"value": "41200", "unit": "minor", "currency": "USD"},
    "status": "settled",
    "rail_ref": {"payment_intent": "pi_3QxAbCdEfGhIjKlM"},
}
EVENT = {"type": "payment.settled", "payment": PAYMENT, "payee": {"type": "merchant", "name": "Example Airways"}}
CONTEXT: dict[str, Any] = {
    "agent": {"id": "agt_travel_12"},
    "owner": {"id": "own_u_4410", "type": "person"},
}


def collect() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [], []


def test_malformed_event_is_dropped_and_reported_not_signed() -> None:
    sent, errors = collect()
    client = HalvorClient(
        source_key=generate_source_key(), sink=lambda e: sent.extend(e), on_error=errors.append
    )
    client.emit({"type": "payment.settled", "payment": {"amount": "lots"}})
    client.flush()
    assert sent == []
    assert client.stats().dropped_invalid == 1
    assert client.stats().accepted == 0
    assert errors[0]["code"] == "invalid_event"
    assert errors[0]["details"]


def test_valid_event_passes_and_is_signed() -> None:
    sent, errors = collect()
    client = HalvorClient(
        source_key=generate_source_key(), sink=lambda e: sent.extend(e), on_error=errors.append
    )
    with agent_context(**CONTEXT):
        client.emit(EVENT)
    client.flush()
    assert errors == []
    assert len(sent) == 1
    assert validate_event(sent[0]["event"]) == []


@pytest.mark.parametrize(
    ("mutate", "where"),
    [
        (lambda e: e["payment"].update(status="failed"), "failure_reason"),
        (lambda e: e["payment"].pop("rail_ref"), "rail_ref"),
        (lambda e: e["payee"].pop("name"), "payee"),
        (lambda e: e["payment"]["amount"].update(value="41.20"), "value"),
        (lambda e: e.update(type="payment.approved"), "type"),
    ],
)
def test_rejects_what_the_schema_rejects(mutate: Any, where: str) -> None:
    sent, errors = collect()
    client = HalvorClient(
        source_key=generate_source_key(), sink=lambda e: sent.extend(e), on_error=errors.append
    )
    event = json.loads(json.dumps(EVENT))
    mutate(event)
    with agent_context(**CONTEXT):
        client.emit(event)
    client.flush()
    assert sent == []
    assert where in " ".join(errors[0]["details"])


def test_card_numbers_are_redacted_before_validation_like_the_typescript_sdk() -> None:
    sent, errors = collect()
    client = HalvorClient(
        source_key=generate_source_key(), sink=lambda e: sent.extend(e), on_error=errors.append
    )
    event = json.loads(json.dumps(EVENT))
    event["payee"]["name"] = "Airways card 4242 4242 4242 4242"
    with agent_context(**CONTEXT):
        client.emit(event)
    client.flush()
    assert errors == []
    assert "4242 4242 4242 4242" not in json.dumps(sent)


def test_test_keys_go_to_the_sandbox_and_live_keys_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HALVOR_ENDPOINT", raising=False)
    test = HalvorClient(api_key="hk_test_" + "a" * 32, source_key=generate_source_key())
    live = HalvorClient(api_key="hk_live_" + "a" * 32, source_key=generate_source_key())
    assert test.endpoint == "https://api.sandbox.halvorai.com"
    assert live.endpoint == "https://api.halvorai.com"
    assert live.environment == "live"
    assert HalvorClient(environment="test").endpoint == "https://api.sandbox.halvorai.com"


def test_endpoint_is_a_base_url_like_the_typescript_sdk() -> None:
    assert events_url("http://127.0.0.1:8787") == "http://127.0.0.1:8787/v1/events"
    assert events_url("http://127.0.0.1:8787/") == "http://127.0.0.1:8787/v1/events"
    assert events_url("http://127.0.0.1:8787/v1/events") == "http://127.0.0.1:8787/v1/events"


def test_reads_keys_and_endpoint_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    key = generate_source_key(kid="src_env")
    monkeypatch.setenv("HALVOR_API_KEY", "hk_test_" + "b" * 32)
    monkeypatch.setenv("HALVOR_SOURCE_KEY", json.dumps(key))
    monkeypatch.setenv("HALVOR_ENDPOINT", "http://127.0.0.1:8787")
    client = HalvorClient()
    assert client.api_key == "hk_test_" + "b" * 32
    assert client.endpoint == "http://127.0.0.1:8787"
    assert client._source_id == "src_env"


def test_counts_each_event_by_the_status_the_api_returned() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        ids = [e["event"]["id"] for e in json.loads(request.content)["events"]]
        results = [
            {"id": ids[0], "status": "accepted", "chain_seq": 1},
            {"id": ids[1], "status": "duplicate"},
            {"id": ids[2], "status": "rejected", "error": {"code": "invalid_signature"}},
        ]
        return httpx.Response(202, json={"results": results})

    errors: list[dict[str, Any]] = []
    client = HalvorClient(
        api_key="hk_test_" + "c" * 32,
        source_key=generate_source_key(),
        endpoint="http://127.0.0.1:8787",
        transport=httpx.MockTransport(handler),
        on_error=errors.append,
    )
    with agent_context(**CONTEXT):
        for _ in range(3):
            client.emit(EVENT)
    client.flush()
    stats = client.stats()
    assert seen["url"] == "http://127.0.0.1:8787/v1/events"
    assert (stats.accepted, stats.duplicates, stats.rejected) == (1, 1, 1)
    assert errors == [
        {
            "code": "rejected",
            "message": "server rejected event",
            "eventId": errors[0]["eventId"],
            "serverCode": "invalid_signature",
        }
    ]


def test_no_api_key_counts_the_events_it_could_not_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HALVOR_API_KEY", raising=False)
    errors: list[dict[str, Any]] = []
    client = HalvorClient(source_key=generate_source_key(), on_error=errors.append)
    with agent_context(**CONTEXT):
        client.emit(EVENT)
    client.flush()
    assert client.stats().dropped_disabled == 1
    assert errors[0]["code"] == "config"
