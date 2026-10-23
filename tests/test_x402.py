"""x402 mapping and wrappers, using the same fixtures as the Node.js SDK (PY-4 to PY-7, PY-14, PY-15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from halvorai_ingest import HalvorClient, agent_context, generate_source_key
from halvorai_ingest.validate import validate_event
from halvorai_ingest.x402 import (
    AsyncHalvorX402Transport,
    HalvorX402Transport,
    map_payment_payload,
    map_payment_required,
    map_settlement_response,
    wrap_httpx,
    wrap_httpx_async,
)
from halvorai_ingest.x402.decode import encode_payment_header

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "x402"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


REQUIRED = load("payment-required.json")
PAYLOAD = load("payment-payload.json")
SETTLED = load("settlement-success.json")
FAILED = load("settlement-failure.json")
NONCE = PAYLOAD["payload"]["authorization"]["nonce"]
ACCEPT = PAYLOAD["accepted"]


def emit_all(*mapped: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Run mapped events through a real client and return the signed events."""
    sent: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    client = HalvorClient(source_key=generate_source_key(), sink=sent.extend, on_error=errors.append, exit_hook=False)
    for m in mapped:
        assert m is not None
        client.emit(m)
    client.flush(timeout=5)
    client.close()
    assert errors == []
    return [e["event"] for e in sent]


def test_matches_the_node_sdk_for_the_shared_fixtures() -> None:
    requested, authorized, settled = emit_all(
        map_payment_required(REQUIRED),
        map_payment_payload(PAYLOAD),
        map_settlement_response(SETTLED, accept=ACCEPT, nonce=NONCE, resource=PAYLOAD["resource"]),
    )
    for event in (requested, authorized, settled):
        assert validate_event(event) == []

    assert requested["payment"]["payment_id"] == f"x402:eip155:84532:requested:{requested['id']}"
    assert requested["payment"]["rail_ref"] == {"scheme": "exact", "network": "eip155:84532"}
    assert requested["payment"]["description"] == "Access to premium market data"
    assert requested["payee"] == {
        "type": "api_provider",
        "name": "Access to premium market data",
        "resource_url": "https://api.example.com/premium-data",
        "wallet": {"network": "eip155:84532", "address": ACCEPT["payTo"]},
    }
    assert requested["payment"]["amount"]["asset"] == {
        "network": "eip155:84532",
        "address": ACCEPT["asset"],
        "symbol": "USDC",
    }

    # PY-4: authorization and settlement share one payment id, built from the nonce.
    assert authorized["payment"]["payment_id"] == settled["payment"]["payment_id"] == f"x402:eip155:84532:{NONCE}"
    assert settled["payment"]["rail_ref"]["tx_hash"] == SETTLED["transaction"]
    assert authorized["instrument"] == {
        "type": "wallet",
        "wallet": {"network": "eip155:84532", "address": PAYLOAD["payload"]["authorization"]["from"]},
    }
    assert authorized["payee"] == requested["payee"]


def test_failed_settlement_has_no_fake_tx_hash_and_a_bounded_reason() -> None:
    long_reason = {**FAILED, "errorReason": "x" * 500}
    (failed,) = emit_all(map_settlement_response(long_reason, accept=ACCEPT, nonce=NONCE))
    assert failed["type"] == "payment.failed"
    assert "tx_hash" not in failed["payment"]["rail_ref"]
    assert len(failed["payment"]["failure_reason"]) == 128


def test_payee_without_a_resource_or_pay_to_still_validates() -> None:
    bare = {"accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "1", "asset": "0xabc"}]}
    (event,) = emit_all(map_payment_required(bare))
    assert event["payee"] == {"type": "wallet", "name": "x402 payee"}


def test_context_details_reach_the_event() -> None:
    with agent_context(
        agent={"id": "agt_1", "name": "Research agent", "framework": "langgraph"},
        owner={"id": "own_1", "type": "person", "country": "US"},
    ):
        mapped = map_payment_payload(PAYLOAD)
        assert mapped is not None and "agent" not in mapped and "owner" not in mapped
        (event,) = emit_all(mapped)
    assert event["agent"] == {"id": "agt_1", "name": "Research agent", "framework": "langgraph"}
    assert event["owner"]["country"] == "US"


def test_payer_wallet_is_the_agent_without_context() -> None:
    mapped = map_payment_payload(PAYLOAD)
    assert mapped is not None
    assert mapped["agent"] == {"id": PAYLOAD["payload"]["authorization"]["from"]}


@pytest.mark.parametrize("missing", ["network", "asset"])
def test_never_guesses_a_missing_network_or_asset(missing: str) -> None:
    accept = {k: v for k, v in REQUIRED["accepts"][0].items() if k != missing}
    with pytest.raises(ValueError, match=f"no {missing}"):
        map_payment_required({**REQUIRED, "accepts": [accept]})


def test_every_challenge_gets_its_own_id() -> None:
    a = map_payment_required(REQUIRED)
    b = map_payment_required(REQUIRED)
    assert a is not None and b is not None
    assert a["payment"]["payment_id"] != b["payment"]["payment_id"]


def x402_server(request: httpx.Request) -> httpx.Response:
    """402 with a challenge, then 200 with a settlement once the payment header is sent."""
    if "PAYMENT-SIGNATURE" not in request.headers:
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": encode_payment_header(REQUIRED)})
    return httpx.Response(200, json={"ok": True}, headers={"PAYMENT-RESPONSE": encode_payment_header(SETTLED)})


def recording_client() -> tuple[HalvorClient, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    return HalvorClient(source_key=generate_source_key(), sink=sent.extend, exit_hook=False), sent


def run_round_trip_sync(http: Any) -> None:
    url = "https://api.example.com/premium-data"
    http.get(url)
    http.put(url, headers={"PAYMENT-SIGNATURE": encode_payment_header(PAYLOAD)})


def check_round_trip(halvor: HalvorClient, sent: list[dict[str, Any]]) -> None:
    halvor.flush(timeout=5)
    halvor.close()
    events = [e["event"] for e in sent]
    assert [e["type"] for e in events] == ["payment.requested", "payment.authorized", "payment.settled"]
    requested, authorized, settled = events
    assert authorized["related"] == {"previous_event_id": requested["id"]}
    assert authorized["payment"]["payment_id"] == settled["payment"]["payment_id"]


def test_sync_transport_sees_every_method() -> None:
    halvor, sent = recording_client()
    with httpx.Client(transport=HalvorX402Transport(halvor, httpx.MockTransport(x402_server))) as http:
        run_round_trip_sync(http)
    check_round_trip(halvor, sent)


def test_wrap_httpx_covers_put() -> None:
    halvor, sent = recording_client()
    http = wrap_httpx(httpx.Client(transport=httpx.MockTransport(x402_server)), halvor)
    run_round_trip_sync(http)
    http.close()
    check_round_trip(halvor, sent)


async def test_async_transport_and_wrapper() -> None:
    url = "https://api.example.com/premium-data"
    for make in ("transport", "wrapper"):
        halvor, sent = recording_client()
        if make == "transport":
            http: Any = httpx.AsyncClient(transport=AsyncHalvorX402Transport(halvor, httpx.MockTransport(x402_server)))
        else:
            http = wrap_httpx_async(httpx.AsyncClient(transport=httpx.MockTransport(x402_server)), halvor)
        await http.get(url)
        await http.post(url, headers={"PAYMENT-SIGNATURE": encode_payment_header(PAYLOAD)})
        await http.aclose()
        check_round_trip(halvor, sent)
