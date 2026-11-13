"""Stripe adapter: map Stripe and Stripe Issuing webhooks to APE events.

    from halvorai_ingest.stripe import tag_issuing_card

    event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)  # your check
    halvor.stripe(event)

`halvor.stripe(event)` maps the event and queues it, returning the event id, or `None` for a
type the adapter does not handle. `map_stripe_event` returns the partial APE event without
queuing it. Verify the Stripe signature yourself first; the adapter trusts what it is given.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from .map import STRIPE_HANDLED_TYPES, map_stripe_event
from .tag import tag_issuing_card, tag_payment_intent

if TYPE_CHECKING:
    from ..client import HalvorClient


def stripe_webhook_handler(client: HalvorClient) -> Callable[[Any], str | None]:
    """Return a handler that maps Stripe events through `client`. Use after your own signature check."""
    return client.stripe


__all__ = [
    "STRIPE_HANDLED_TYPES",
    "map_stripe_event",
    "tag_payment_intent",
    "tag_issuing_card",
    "stripe_webhook_handler",
]
