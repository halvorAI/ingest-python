from __future__ import annotations

import json
from typing import Any
from pathlib import Path

from halvorai_ingest import HalvorClient, agent_context, verify_event

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


def test_unknown_owner_is_labelled_because_the_type_is_only_a_placeholder() -> None:
    """halvorAI/halvorai-app#99: v0.1 forces a type, so the label says it is not a fact."""
    event = _emit_one()
    assert event["owner"] == {"id": "unattributed", "type": "business"}
    assert event["labels"] == {"owner_type": "unknown"}


def test_an_adapter_supplied_unattributed_owner_is_labelled_too() -> None:
    event = _emit_one(owner={"id": "unattributed", "type": "business"})
    assert event["labels"] == {"owner_type": "unknown"}


def test_a_real_owner_is_not_labelled() -> None:
    event = _emit_one(owner={"id": "own_real", "type": "person"})
    assert "labels" not in event


def test_caller_labels_are_kept_and_owner_type_is_not_overwritten() -> None:
    assert _emit_one(labels={"team": "growth"})["labels"] == {"team": "growth", "owner_type": "unknown"}
    assert _emit_one(labels={"owner_type": "person"})["labels"] == {"owner_type": "person"}
