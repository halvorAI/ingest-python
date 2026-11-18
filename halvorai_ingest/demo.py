"""`halvorai-ingest demo`: sign one event offline, verify it, then show a one-byte change fail."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .client import HalvorClient
from .context import agent_context
from .sign import generate_source_key, verify_event


@dataclass(frozen=True)
class DemoResult:
    signed_verified: bool
    tampered_rejected: bool


def run_demo(log: Callable[[str], None] = print) -> DemoResult:
    source_key = generate_source_key()
    public_key = {k: v for k, v in source_key.items() if k != "d"}
    envelopes: list[dict[str, Any]] = []
    halvor = HalvorClient(source_key=source_key, environment="test", sink=envelopes.extend)
    try:
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
        halvor.flush(timeout=5)
    finally:
        halvor.close(timeout=1)
    if not envelopes:
        raise RuntimeError("no event was signed")
    event, sig = envelopes[0]["event"], envelopes[0]["sig"]

    log("1. Sign an event on this machine")
    log(f"   {event['id']}: Travel booking agent paid Example Airways 412.00 USD on stripe")
    log(f"   Instruction sent as {event['instruction']['hash'][:19]}…, never as text")
    signed_verified = verify_event(event, sig, public_key)
    log(f"   Ed25519 signature: {'verified' if signed_verified else 'NOT verified'}")
    log("")

    log("2. Change the amount from 41200 to 41290 and verify again")
    tampered = copy.deepcopy(event)
    tampered["payment"]["amount"]["value"] = "41290"
    tampered_ok = verify_event(tampered, sig, public_key)
    log(f"   Ed25519 signature: {'verified' if tampered_ok else 'FAILED, the event was changed after signing'}")
    log("")
    log("Next: pass sink=... to keep envelopes, or run `halvorai-ingest init` with a sandbox key.")
    return DemoResult(signed_verified=signed_verified, tampered_rejected=not tampered_ok)
