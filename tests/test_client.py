from __future__ import annotations

import json
from typing import Any
from pathlib import Path

from halvorai_ingest import HalvorClient, agent_context, verify_event
from halvorai_ingest.hash import hash_instruction

VECTOR = Path(__file__).resolve().parent / "fixtures" / "sign-vector.json"


def test_emit_flush_sink_signs() -> None:
    data = json.loads(VECTOR.read_text(encoding="utf-8"))
    envelopes: list[dict[str, Any]] = []
    client = HalvorClient(
        source_key=data["private_jwk"],
        environment="test",
        sink=lambda batch: envelopes.extend(batch),
    )
    with agent_context(
        agent={"id": "agt_py"},
        owner={"id": "own_py", "type": "business"},
        instruction={"id": "ins_1", "text": "pay it"},
    ):
        client.emit(
            {
                "type": "payment.settled",
                "payment": {
                    "payment_id": "py:1",
                    "rail": "x402",
                    "amount": {
                        "value": "10000",
                        "unit": "atomic",
                        "asset": {
                            "network": "eip155:84532",
                            "address": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                            "symbol": "USDC",
                            "decimals": 6,
                        },
                    },
                    "status": "settled",
                "rail_ref": {"scheme": "exact", "network": "eip155:8453"},
                    "rail_ref": {"scheme": "exact", "network": "eip155:84532"},
                },
                "payee": {"type": "api_provider", "name": "demo"},
                "source": {"adapter": "x402-client"},
            }
        )
    client.flush()
    client.close()
    assert len(envelopes) == 1
    event = envelopes[0]["event"]
    sig = envelopes[0]["sig"]
    assert event["agent"]["id"] == "agt_py"
    assert event["instruction"]["hash"].startswith("sha256:")
    assert verify_event(event, sig, data["public_jwk"]) is True


def test_emit_never_throws() -> None:
    client = HalvorClient(source_key=None)
    assert client.emit(None)
    assert client.emit("bad")


def _emit_one(**overrides: Any) -> dict[str, Any]:
    data = json.loads(VECTOR.read_text(encoding="utf-8"))
    envelopes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    client = HalvorClient(source_key=data["private_jwk"], environment="test", sink=envelopes.extend, on_error=errors.append, exit_hook=False)
    client.emit(
        {
            "type": "payment.settled",
            "payment": {
                "payment_id": "py:1",
                "rail": "x402",
                "status": "settled",
                "rail_ref": {"scheme": "exact", "network": "eip155:8453"},
                "amount": {"value": "1", "unit": "atomic", "asset": {"network": "eip155:8453", "address": "0xabc", "symbol": "USDC", "decimals": 6}},
            },
            "payee": {"type": "wallet", "name": "x402 payee"},
            **overrides,
        }
    )
    client.flush(timeout=5)
    client.close()
    assert errors == []
    (envelope,) = envelopes
    event: dict[str, Any] = envelope["event"]
    return event


def test_unknown_owner_is_unknown_with_no_label_stopgap() -> None:
    """halvorAI/halvorai-app#99: APE v0.2 says the owner type is unknown."""
    event = _emit_one()
    assert event["owner"] == {"id": "unattributed", "type": "unknown", "unknown_reason": "not_provided"}
    assert "labels" not in event
    assert event["ape_version"] == "0.2"


def test_a_fully_attributed_event_stays_v01() -> None:
    event = _emit_one(agent={"id": "agt_1"}, owner={"id": "own_real", "type": "person"})
    assert event["ape_version"] == "0.1"
    assert "labels" not in event


def test_v02_fields_pass_through() -> None:
    event = _emit_one(
        agent={"id": "agt_1"},
        owner={"id": "own_1", "type": "person"},
        source={"perspective": "payer"},
        delivery={"http_status": 200, "summary": "10 of 12 results"},
        attribution_gaps=[{"field": "mandate", "reason": "not_provided"}],
    )
    assert event["ape_version"] == "0.2"
    assert event["source"]["perspective"] == "payer"
    assert event["delivery"] == {"http_status": 200, "summary": "10 of 12 results"}


def test_normalise_adds_a_ws_lower_v1_hash() -> None:
    event = _emit_one(instruction={"id": "ins_1", "text": "Buy it from http://localhost:8123/pay", "normalise": True})
    assert event["instruction"]["normalisation"] == "ws-lower-v1"
    assert event["instruction"]["normalised_hash"] == hash_instruction("buy it from /pay")
    assert event["instruction"]["hash"] == hash_instruction("Buy it from http://localhost:8123/pay")


def test_emit_report_hashes_the_message_and_has_no_payment() -> None:
    data = json.loads(VECTOR.read_text(encoding="utf-8"))
    envelopes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    client = HalvorClient(source_key=data["private_jwk"], environment="test", sink=envelopes.extend, on_error=errors.append, exit_hook=False)
    client.emit_report(
        instruction={"id": "ins_1", "text": "Buy one export"},
        report={"body": "Done, paid once.", "payments_claimed": 1, "summary": "Paid once."},
    )
    client.flush(timeout=5)
    client.close()
    assert errors == []
    (envelope,) = envelopes
    event = envelope["event"]
    assert event["type"] == "agent.reported"
    assert event["ape_version"] == "0.2"
    assert event["report"]["body_sha256"] == hash_instruction("Done, paid once.")
    assert "payment" not in event
    assert "Done, paid once." not in json.dumps(event)


