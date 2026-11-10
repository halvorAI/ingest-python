"""Stripe adapter, using the same fixtures as the Node.js SDK.

tests/fixtures/stripe/*.json are copied from the Node.js SDK's Stripe fixtures, and expected.json
is what the Node.js `mapStripeEvent` returns for each of them, so both SDKs map Stripe alike.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from halvorai_ingest import HalvorClient, agent_context, generate_source_key
from halvorai_ingest.stripe import (
    STRIPE_HANDLED_TYPES,
    map_stripe_event,
    stripe_webhook_handler,
    tag_issuing_card,
    tag_payment_intent,
)
from halvorai_ingest.validate import validate_event

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "stripe"
EXPECTED: dict[str, Any] = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
FILES = sorted(p.name for p in FIXTURES.glob("*.json") if p.name != "expected.json")


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def client_with(sent: list[dict[str, Any]], errors: list[dict[str, Any]]) -> HalvorClient:
    return HalvorClient(source_key=generate_source_key(), sink=sent.extend, on_error=errors.append, exit_hook=False)


@pytest.mark.parametrize("name", FILES)
def test_matches_the_node_sdk(name: str) -> None:
    assert map_stripe_event(load(name)) == EXPECTED[name]


@pytest.mark.parametrize("case", [k for k in EXPECTED if k.startswith("case:")])
def test_matches_the_node_sdk_edge_cases(case: str) -> None:
    assert map_stripe_event(EXPECTED[case]["input"]) == EXPECTED[case]["output"]


@pytest.mark.parametrize(
    ("name", "ape_type", "status", "adapter"),
    [
        ("payment_intent.amount_capturable_updated.json", "payment.authorized", "authorized", "stripe"),
        ("payment_intent.succeeded.json", "payment.settled", "settled", "stripe"),
        ("payment_intent.payment_failed.json", "payment.failed", "failed", "stripe"),
        ("charge.refunded.json", "payment.refunded", "refunded", "stripe"),
        ("charge.dispute.created.json", "payment.disputed", "disputed", "stripe"),
        ("issuing_authorization.created.json", "payment.authorized", "authorized", "stripe-issuing"),
        ("issuing_authorization.updated.json", "payment.failed", "failed", "stripe-issuing"),
        ("issuing_transaction.created.json", "payment.settled", "settled", "stripe-issuing"),
        ("issuing_transaction.refund.json", "payment.refunded", "refunded", "stripe-issuing"),
    ],
)
def test_types(name: str, ape_type: str, status: str, adapter: str) -> None:
    mapped = map_stripe_event(load(name))
    assert mapped is not None
    assert (mapped["type"], mapped["payment"]["status"], mapped["source"]["adapter"]) == (ape_type, status, adapter)


def test_ignores_unrelated_types_and_bad_input() -> None:
    assert map_stripe_event({"type": "customer.created", "data": {"object": {"id": "cus_x"}}}) is None
    assert map_stripe_event({"type": "payment_intent.succeeded", "data": {"object": "pi_x"}}) is None
    assert map_stripe_event("not an event") is None
    assert len(STRIPE_HANDLED_TYPES) == 8


def test_unattributed_without_metadata() -> None:
    mapped = map_stripe_event(load("unattributed.json"))
    assert mapped is not None
    assert mapped["agent"]["id"] == "unattributed"
    assert mapped["owner"]["id"] == "unattributed"


def test_issuing_agent_name_and_framework_from_card_metadata() -> None:
    event = load("issuing_authorization.created.json")
    event["data"]["object"]["card"]["metadata"].update(
        halvor_agent_name="Travel booking agent", halvor_agent_framework="errandly-agents"
    )
    named = map_stripe_event(event)
    assert named is not None
    assert named["agent"] == {"id": "agt_travel_12", "name": "Travel booking agent", "framework": "errandly-agents"}
    plain = map_stripe_event(load("issuing_authorization.created.json"))
    assert plain is not None
    assert plain["agent"] == {"id": "agt_travel_12"}


class _StripeObjectLike:
    """Stands in for stripe.Event, which converts with to_dict()."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return self._data


def test_accepts_stripe_objects() -> None:
    name = "payment_intent.succeeded.json"
    assert map_stripe_event(_StripeObjectLike(load(name))) == EXPECTED[name]


@pytest.mark.parametrize("name", FILES)
def test_every_fixture_is_a_valid_signed_event(name: str) -> None:
    sent: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    client = client_with(sent, errors)
    event_id = client.stripe(load(name))
    client.flush(timeout=5)
    client.close()
    assert errors == []
    assert len(sent) == 1
    event = sent[0]["event"]
    assert event["id"] == event_id
    assert validate_event(event) == []
    assert event["source"]["adapter"] == EXPECTED[name]["source"]["adapter"]


def test_client_method_and_handler() -> None:
    sent: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    client = client_with(sent, errors)
    handler = stripe_webhook_handler(client)
    assert handler({"type": "invoice.paid", "data": {"object": {}}}) is None
    event_id = handler(load("payment_intent.succeeded.json"))
    assert isinstance(event_id, str) and event_id.startswith("evt_")
    client.flush(timeout=5)
    client.close()
    assert errors == []
    payment = sent[0]["event"]["payment"]
    assert sent[0]["event"]["type"] == "payment.settled"
    assert payment["payment_id"] == "pi_3QxAbCdEfGhIjKlM"
    assert payment["amount"] == {"value": "41200", "unit": "minor", "currency": "USD"}


def test_tag_payment_intent_from_context() -> None:
    with agent_context(
        agent={"id": "agt_ctx_1", "name": "Ctx"},
        owner={"id": "own_ctx_1", "type": "business"},
        instruction={"id": "ins_ctx_1", "text": "buy coffee"},
    ):
        tagged = tag_payment_intent({"amount": 500, "currency": "usd", "metadata": {"order": "1"}})
    assert tagged["amount"] == 500
    assert tagged["metadata"] == {
        "order": "1",
        "halvor_agent_id": "agt_ctx_1",
        "halvor_agent_name": "Ctx",
        "halvor_owner_id": "own_ctx_1",
        "halvor_owner_type": "business",
        "halvor_instruction_id": "ins_ctx_1",
    }


def test_tag_issuing_card_overrides_win_over_context() -> None:
    params = {"currency": "usd", "type": "virtual"}
    with agent_context(agent={"id": "agt_outer"}, owner={"id": "own_outer", "type": "person"}):
        tagged = tag_issuing_card(params, agent_id="agt_override")
    assert tagged["metadata"]["halvor_agent_id"] == "agt_override"
    assert tagged["metadata"]["halvor_owner_id"] == "own_outer"
    assert "metadata" not in params  # the input is not changed


def test_tag_issuing_card_name_and_framework_within_ape_limits() -> None:
    with agent_context(
        agent={"id": "agt_ctx", "name": "Shopping agent", "framework": "errandly-agents"},
        owner={"id": "own_ctx", "type": "business"},
    ):
        from_ctx = tag_issuing_card({"currency": "usd", "type": "virtual"})
    assert from_ctx["metadata"]["halvor_agent_name"] == "Shopping agent"
    assert from_ctx["metadata"]["halvor_agent_framework"] == "errandly-agents"

    long = tag_issuing_card({"currency": "usd"}, agent_id="agt_x", agent_name="n" * 300, framework="f" * 100)
    assert len(long["metadata"]["halvor_agent_name"]) == 128
    assert len(long["metadata"]["halvor_agent_framework"]) == 64
    assert all(len(k) <= 40 for k in long["metadata"])


def test_tagged_card_round_trips_to_a_named_agent() -> None:
    meta = tag_issuing_card(
        {}, agent_id="agt_rt", agent_name="Round trip", framework="fw", owner_id="own_rt", owner_type="person"
    )["metadata"]
    event = load("issuing_transaction.created.json")
    event["data"]["object"]["card"] = {"metadata": meta}
    mapped = map_stripe_event(event)
    assert mapped is not None
    assert mapped["agent"] == {"id": "agt_rt", "name": "Round trip", "framework": "fw"}
    assert mapped["owner"] == {"id": "own_rt", "type": "person"}
