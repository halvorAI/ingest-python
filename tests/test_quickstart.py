"""The README offline quickstart: generate a key, emit inside agent_context, verify the envelope."""

from __future__ import annotations

from typing import Any

from halvorai_ingest import HalvorClient, agent_context, generate_source_key, verify_event


def test_offline_quickstart_round_trip() -> None:
    source_key = generate_source_key()
    public_key = {k: v for k, v in source_key.items() if k != "d"}
    envelopes: list[dict[str, Any]] = []
    halvor = HalvorClient(source_key=source_key, environment="test", sink=envelopes.extend)

    with agent_context(
        agent={"id": "agt_travel_12", "name": "Travel booking agent"},
        owner={"id": "own_u_4410", "type": "person", "country": "US"},
        instruction={"id": "task_8890", "text": "Book a return flight to Austin under $500"},
    ):
        halvor.emit(
            {
                "type": "payment.settled",
                "payment": {
                    "payment_id": "pi_3QxAbCdEfGhIjKlM",
                    "rail": "stripe",
                    "amount": {"value": "41200", "unit": "minor", "currency": "USD"},
                    "status": "settled",
                    "rail_ref": {"payment_intent": "pi_3QxAbCdEfGhIjKlM"},
                },
                "payee": {"type": "merchant", "name": "Example Airways"},
            }
        )
    halvor.flush()

    assert len(envelopes) == 1
    event = envelopes[0]["event"]
    assert event["source"] == {"id": "src_local", "sdk": "halvorai-ingest@0.1.0", "adapter": "custom"}
    assert event["agent"]["id"] == "agt_travel_12"
    assert "text" not in event["instruction"]
    assert event["instruction"]["hash"].startswith("sha256:")
    assert verify_event(event, envelopes[0]["sig"], public_key) is True


def test_generate_source_key_is_ed25519_jwk() -> None:
    key = generate_source_key("src_abc")
    assert key["kty"] == "OKP" and key["crv"] == "Ed25519" and key["kid"] == "src_abc"
    assert generate_source_key()["d"] != key["d"]
