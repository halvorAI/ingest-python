"""Map Stripe webhook events to partial APE events, the same way as the Node.js and Go SDKs.

Handled types (anything else maps to `None`):

- `payment_intent.amount_capturable_updated`, `payment_intent.succeeded`,
  `payment_intent.payment_failed`, `charge.refunded`, `charge.dispute.created` (rail `stripe`)
- `issuing_authorization.created`, `issuing_authorization.updated`,
  `issuing_transaction.created` (rail `card`, adapter `stripe-issuing`)

Attribution comes from `metadata.halvor_*` on the object, and for Issuing also on the card,
as written by `tag_payment_intent` and `tag_issuing_card`. Missing metadata maps to
`unattributed`. No Stripe SDK dependency: events are plain dicts, or Stripe objects that
convert to one (`stripe.Event` has `to_dict()`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

from ..hash import evidence_sha256, hash_instruction

STRIPE_HANDLED_TYPES: tuple[str, ...] = (
    "payment_intent.amount_capturable_updated",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.refunded",
    "charge.dispute.created",
    "issuing_authorization.created",
    "issuing_authorization.updated",
    "issuing_transaction.created",
)

_CURRENCY = re.compile(r"^[A-Z]{3}$")
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")
_MCC = re.compile(r"^[0-9]{4}$")
_URL = re.compile(r"^https?://", re.IGNORECASE)


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)  # stripe.StripeObject
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _num(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def _currency(raw: Any) -> str:
    c = _str(raw)
    c = c.upper() if c is not None else None
    return c if c is not None and _CURRENCY.match(c) else "USD"


def _minor(amount: int | float, currency: str) -> dict[str, str]:
    return {"value": str(int(amount)), "unit": "minor", "currency": currency}


def _occurred_at(event: Mapping[str, Any]) -> str | None:
    created = _num(event.get("created"))
    if created is None:
        return None
    dt = datetime.fromtimestamp(created, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _metadata(obj: Mapping[str, Any]) -> dict[str, str]:
    meta = _as_dict(obj.get("metadata"))
    if meta is None:
        return {}
    return {k: v for k, v in meta.items() if isinstance(v, str)}


def _card_metadata(obj: Mapping[str, Any]) -> dict[str, str]:
    card = _as_dict(obj.get("card"))
    return {} if card is None else _metadata(card)


def _attribution(meta: Mapping[str, str]) -> dict[str, Any]:
    agent: dict[str, Any] = {"id": meta.get("halvor_agent_id", "unattributed")}
    name = meta.get("halvor_agent_name")
    framework = meta.get("halvor_agent_framework")
    if name:
        agent["name"] = name[:128]
    if framework:
        agent["framework"] = framework[:64]
    out: dict[str, Any] = {
        "agent": agent,
        "owner": {
            "id": meta.get("halvor_owner_id", "unattributed"),
            "type": "person" if meta.get("halvor_owner_type") == "person" else "business",
        },
    }
    instruction_id = meta.get("halvor_instruction_id")
    if instruction_id is not None:
        # Webhooks carry only the id; hash a stable local stand-in so APE validation passes.
        out["instruction"] = {"id": instruction_id, "hash": hash_instruction(f"stripe-meta:{instruction_id}")}
    return out


def _payee_from_connect(obj: Mapping[str, Any]) -> dict[str, Any]:
    transfer = _as_dict(obj.get("transfer_data"))
    destination = (_str(transfer.get("destination")) if transfer else None) or _str(obj.get("on_behalf_of"))
    if destination is not None:
        return {"type": "merchant", "id": destination}
    description = _str(obj.get("description"))
    if description is not None:
        return {"type": "merchant", "name": description[:256]}
    return {"type": "merchant", "name": "Stripe merchant"}


def _payee_from_merchant_data(obj: Mapping[str, Any]) -> dict[str, Any]:
    md = _as_dict(obj.get("merchant_data"))
    if md is None:
        return {"type": "merchant", "name": "Card merchant"}
    payee: dict[str, Any] = {"type": "merchant"}
    name = _str(md.get("name"))
    pid = _str(md.get("network_id"))
    country = _str(md.get("country"))
    mcc = _str(md.get("category_code"))
    url = _str(md.get("url"))
    if name is not None:
        payee["name"] = name
    if pid is not None:
        payee["id"] = pid
    if country is not None and _COUNTRY.match(country):
        payee["country"] = country.upper()
    if mcc is not None and _MCC.match(mcc):
        payee["mcc"] = mcc
    if url is not None and _URL.match(url):
        payee["website"] = url
    if "name" not in payee and "id" not in payee:
        payee["name"] = "Card merchant"
    return payee


def _evidence(obj: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"kind": "stripe.object", "format": "json", "value": obj, "sha256": evidence_sha256(obj)}]


def _shared_payment_token(obj: Mapping[str, Any]) -> str | None:
    meta = _metadata(obj)
    return meta.get("shared_payment_token") or meta.get("halvor_shared_payment_token") or _str(
        obj.get("shared_payment_token")
    )


def _finish(
    event: Mapping[str, Any],
    etype: str,
    adapter: str,
    attr: dict[str, Any],
    payment: dict[str, Any],
    payee: dict[str, Any],
    obj: dict[str, Any],
) -> dict[str, Any]:
    mapped: dict[str, Any] = {
        "type": etype,
        "source": {"adapter": adapter},
        "agent": attr["agent"],
        "owner": attr["owner"],
        "payment": payment,
        "payee": payee,
        "evidence": _evidence(obj),
    }
    at = _occurred_at(event)
    if at is not None:
        mapped["occurred_at"] = at
    if "instruction" in attr:
        mapped["instruction"] = attr["instruction"]
    return mapped


def _payment_intent(event: Mapping[str, Any], obj: dict[str, Any], etype: str, status: str) -> dict[str, Any]:
    pid = _str(obj.get("id")) or "pi_unknown"
    attr = _attribution(_metadata(obj))
    rail_ref: dict[str, Any] = {"payment_intent": pid}
    payment: dict[str, Any] = {
        "payment_id": pid,
        "rail": "stripe",
        "amount": _minor(_num(obj.get("amount")) or 0, _currency(obj.get("currency"))),
        "status": status,
        "rail_ref": rail_ref,
    }
    description = _str(obj.get("description"))
    if description is not None:
        payment["description"] = description[:256]
    if status == "failed":
        err = _as_dict(obj.get("last_payment_error"))
        code = _str(err.get("code")) if err else None
        payment["failure_reason"] = (code or "payment_failed")[:128]
    spt = _shared_payment_token(obj)
    if spt is not None:
        rail_ref["shared_payment_token"] = spt
    account = _str(obj.get("on_behalf_of"))
    if account is not None:
        rail_ref["account"] = account
    mapped = _finish(event, etype, "stripe", attr, payment, _payee_from_connect(obj), obj)
    if spt is not None:
        mapped["instrument"] = {"type": "stripe_spt"}
    return mapped


def _charge_refunded(event: Mapping[str, Any], obj: dict[str, Any]) -> dict[str, Any]:
    charge_id = _str(obj.get("id")) or "ch_unknown"
    pi = _str(obj.get("payment_intent"))
    amount = _num(obj.get("amount_refunded"))
    if amount is None:
        amount = _num(obj.get("amount")) or 0
    rail_ref: dict[str, Any] = {"charge": charge_id}
    if pi is not None:
        rail_ref["payment_intent"] = pi
    payment: dict[str, Any] = {
        "payment_id": pi or charge_id,
        "rail": "stripe",
        "amount": _minor(amount, _currency(obj.get("currency"))),
        "status": "refunded",
        "rail_ref": rail_ref,
    }
    description = _str(obj.get("description"))
    if description is not None:
        payment["description"] = description[:256]
    attr = _attribution(_metadata(obj))
    return _finish(event, "payment.refunded", "stripe", attr, payment, _payee_from_connect(obj), obj)


def _dispute(event: Mapping[str, Any], obj: dict[str, Any]) -> dict[str, Any]:
    dispute_id = _str(obj.get("id")) or "dp_unknown"
    pi = _str(obj.get("payment_intent"))
    charge = _str(obj.get("charge"))
    rail_ref: dict[str, Any] = {"dispute": dispute_id}
    if pi is not None:
        rail_ref["payment_intent"] = pi
    if charge is not None:
        rail_ref["charge"] = charge
    payment: dict[str, Any] = {
        "payment_id": pi or charge or dispute_id,
        "rail": "stripe",
        "amount": _minor(_num(obj.get("amount")) or 0, _currency(obj.get("currency"))),
        "status": "disputed",
        "rail_ref": rail_ref,
    }
    reason = _str(obj.get("reason"))
    if reason is not None:
        payment["description"] = reason[:256]
    attr = _attribution(_metadata(obj))
    payee = {"type": "merchant", "name": "Stripe dispute"}
    return _finish(event, "payment.disputed", "stripe", attr, payment, payee, obj)


def _card_payment(payment: dict[str, Any], merchant: Mapping[str, Any]) -> None:
    mcc = _str(merchant.get("mcc"))
    if mcc is not None:
        payment["category"] = f"mcc:{mcc}"
    name = _str(merchant.get("name"))
    if name is not None:
        payment["description"] = name[:256]


def _issuing_authorization(event: Mapping[str, Any], obj: dict[str, Any], failed: bool) -> dict[str, Any]:
    aid = _str(obj.get("id")) or "iauth_unknown"
    attr = _attribution({**_metadata(obj), **_card_metadata(obj)})
    merchant = _payee_from_merchant_data(obj)
    payment: dict[str, Any] = {
        "payment_id": aid,
        "rail": "card",
        "amount": _minor(_num(obj.get("amount")) or 0, _currency(obj.get("currency"))),
        "status": "failed" if failed else "authorized",
        "rail_ref": {"authorization_id": aid, "processor": "stripe-issuing"},
    }
    if failed:
        payment["failure_reason"] = "issuing_authorization_declined"
    _card_payment(payment, merchant)
    etype = "payment.failed" if failed else "payment.authorized"
    mapped = _finish(event, etype, "stripe-issuing", attr, payment, merchant, obj)
    card = _as_dict(obj.get("card"))
    if card is not None:
        instrument: dict[str, Any] = {"type": "card", "issuer": "Stripe Issuing"}
        last4 = _str(card.get("last4"))
        brand = _str(card.get("brand"))
        if last4 is not None and _MCC.match(last4):
            instrument["last4"] = last4
        if brand is not None:
            instrument["network"] = brand.lower()
        mapped["instrument"] = instrument
    return mapped


def _issuing_transaction(event: Mapping[str, Any], obj: dict[str, Any]) -> dict[str, Any]:
    tid = _str(obj.get("id")) or "ipi_unknown"
    refunded = (_str(obj.get("type")) or "capture") == "refund"
    attr = _attribution({**_metadata(obj), **_card_metadata(obj)})
    merchant = _payee_from_merchant_data(obj)
    rail_ref: dict[str, Any] = {"transaction_id": tid, "processor": "stripe-issuing"}
    auth = _str(obj.get("authorization"))
    if auth is not None:
        rail_ref["authorization_id"] = auth
    payment: dict[str, Any] = {
        "payment_id": tid,
        "rail": "card",
        "amount": _minor(abs(_num(obj.get("amount")) or 0), _currency(obj.get("currency"))),
        "status": "refunded" if refunded else "settled",
        "rail_ref": rail_ref,
    }
    _card_payment(payment, merchant)
    etype = "payment.refunded" if refunded else "payment.settled"
    return _finish(event, etype, "stripe-issuing", attr, payment, merchant, obj)


def map_stripe_event(event: Any) -> dict[str, Any] | None:
    """
    Map a Stripe webhook event to a partial APE event for `HalvorClient.emit`.
    Returns `None` for types the adapter does not handle.
    """
    ev = _as_dict(event)
    if ev is None:
        return None
    etype = ev.get("type")
    if etype not in STRIPE_HANDLED_TYPES:
        return None
    data = _as_dict(ev.get("data"))
    obj = _as_dict(data.get("object")) if data else None
    if obj is None:
        return None

    if etype == "payment_intent.amount_capturable_updated":
        return _payment_intent(ev, obj, "payment.authorized", "authorized")
    if etype == "payment_intent.succeeded":
        return _payment_intent(ev, obj, "payment.settled", "settled")
    if etype == "payment_intent.payment_failed":
        return _payment_intent(ev, obj, "payment.failed", "failed")
    if etype == "charge.refunded":
        return _charge_refunded(ev, obj)
    if etype == "charge.dispute.created":
        return _dispute(ev, obj)
    if etype == "issuing_authorization.created":
        return _issuing_authorization(ev, obj, obj.get("approved") is False)
    if etype == "issuing_authorization.updated":
        status = _str(obj.get("status"))
        return _issuing_authorization(ev, obj, obj.get("approved") is False or status in ("reversed", "expired"))
    return _issuing_transaction(ev, obj)
