"""Map x402 v2 HTTP payloads to partial APE events, the same way as the Node.js SDK.

Rules from ape-schema `docs/MAPPINGS.md` §5.1:
- `payment.requested` has `payment_id` `x402:<network>:requested:<event id>`, so every challenge
  is its own payment request.
- every later stage uses `x402:<network>:<nonce>`, never the transaction hash, so authorization,
  settlement and failure join. The authorized event links to its challenge with
  `related.previous_event_id`.
- a missing network or asset is never guessed: the mapper raises and the adapter reports
  `invalid_event` instead of recording a testnet or a zero address.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from ..context import current_context
from ..hash import evidence_sha256
from ..ids import new_event_id


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _host(url: str | None) -> str | None:
    """Host and port, like `URL.host` in Node.js."""
    if not url:
        return None
    try:
        return urlsplit(url).netloc or None
    except ValueError:
        return None


def _attribution(payer_wallet: str | None = None) -> dict[str, Any]:
    """Agent and owner, but only when `agent_context` did not set them.

    When it did, they are left out so the client fills the full context (name, framework,
    country, KYC reference) when the event is emitted.
    """
    ctx = current_context()
    out: dict[str, Any] = {}
    if not ctx or not ctx.get("agent"):
        out["agent"] = {"id": payer_wallet or "unattributed"}
    if not ctx or not ctx.get("owner"):
        out["owner"] = {"id": "unattributed", "type": "business"}
    return out


def _asset(accept: Mapping[str, Any]) -> dict[str, Any]:
    network = _str(accept.get("network"))
    if network is None:
        raise ValueError("x402 payment requirement has no network; event not recorded")
    address = _str(accept.get("asset"))
    if address is None:
        raise ValueError("x402 payment requirement has no asset; event not recorded")
    asset: dict[str, Any] = {"network": network, "address": address}
    extra = _mapping(accept.get("extra"))
    if isinstance(extra.get("name"), str):
        asset["symbol"] = extra["name"][:16]
    decimals = extra.get("decimals")
    if isinstance(decimals, int) and not isinstance(decimals, bool):
        asset["decimals"] = decimals
    return asset


def _amount(accept: Mapping[str, Any], override: str | None = None) -> dict[str, Any]:
    return {"value": override or _str(accept.get("amount")) or "0", "unit": "atomic", "asset": _asset(accept)}


def _payee(
    accept: Mapping[str, Any],
    resource: Mapping[str, Any] | None,
    override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if override is not None:
        return dict(override)
    res = _mapping(resource)
    url = _str(res.get("url"))
    name = _str(res.get("serviceName")) or _str(res.get("description")) or _host(url)
    payee: dict[str, Any] = {"type": "api_provider" if url is not None else "wallet"}
    if name is not None:
        payee["name"] = name[:256]
    if url is not None:
        payee["resource_url"] = url
    pay_to = _str(accept.get("payTo"))
    network = _str(accept.get("network"))
    if pay_to is not None and network is not None:
        payee["wallet"] = {"network": network, "address": pay_to}
    if "name" not in payee and "wallet" not in payee:
        payee["name"] = "x402 payee"
    return payee


def _rail_ref(accept: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ref: dict[str, Any] = {}
    if isinstance(accept.get("scheme"), str):
        ref["scheme"] = accept["scheme"]
    if isinstance(accept.get("network"), str):
        ref["network"] = accept["network"]
    flow = _mapping(accept.get("extra")).get("paymentFlow")
    if isinstance(flow, str):
        ref["payment_flow"] = flow
    if extra:
        ref.update(extra)
    return ref


def _description(payment: dict[str, Any], resource: Mapping[str, Any] | None) -> None:
    description = _str(_mapping(resource).get("description"))
    if description is not None:
        payment["description"] = description[:256]


def map_payment_required(
    required: Mapping[str, Any],
    adapter: str = "x402-client",
    payee_override: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    accepts = required.get("accepts")
    if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], Mapping):
        return None
    accept: Mapping[str, Any] = accepts[0]
    resource = _mapping(required.get("resource"))
    event_id = new_event_id()
    network = _str(accept.get("network")) or "unknown"
    payment: dict[str, Any] = {
        "payment_id": f"x402:{network}:requested:{event_id}",
        "rail": "x402",
        "amount": _amount(accept),
        "status": "requested",
        "rail_ref": _rail_ref(accept),
    }
    _description(payment, resource)
    return {
        "id": event_id,
        "type": "payment.requested",
        "source": {"adapter": adapter},
        **_attribution(),
        "payment": payment,
        "payee": _payee(accept, resource, payee_override),
        "evidence": [],
    }


def map_payment_payload(
    payload: Mapping[str, Any],
    adapter: str = "x402-client",
    payee_override: Mapping[str, Any] | None = None,
    *,
    previous_event_id: str | None = None,
) -> dict[str, Any] | None:
    accepted = payload.get("accepted")
    if not isinstance(accepted, Mapping):
        return None
    auth = _mapping(_mapping(payload.get("payload")).get("authorization"))
    nonce = _str(auth.get("nonce")) or "unknown"
    network = _str(accepted.get("network")) or "unknown"
    payer = _str(auth.get("from"))
    payment: dict[str, Any] = {
        "payment_id": f"x402:{network}:{nonce}",
        "rail": "x402",
        "amount": _amount(accepted, _str(auth.get("value"))),
        "status": "authorized",
        "rail_ref": _rail_ref(accepted, {"nonce": nonce}),
    }
    resource = _mapping(payload.get("resource"))
    _description(payment, resource)
    value = dict(payload)
    mapped: dict[str, Any] = {
        "type": "payment.authorized",
        "source": {"adapter": adapter},
        **_attribution(payer),
        "payment": payment,
        "payee": _payee(accepted, resource, payee_override),
        "evidence": [
            {"kind": "x402.payment_payload", "format": "json", "value": value, "sha256": evidence_sha256(value)}
        ],
    }
    if payer is not None and isinstance(accepted.get("network"), str):
        mapped["instrument"] = {"type": "wallet", "wallet": {"network": accepted["network"], "address": payer}}
    if previous_event_id is not None:
        mapped["related"] = {"previous_event_id": previous_event_id}
    return mapped


def map_settlement_response(
    settlement: Mapping[str, Any],
    *,
    accept: Mapping[str, Any] | None = None,
    nonce: str | None = None,
    resource: Mapping[str, Any] | None = None,
    payee_override: Mapping[str, Any] | None = None,
    adapter: str = "x402-client",
) -> dict[str, Any] | None:
    merged: dict[str, Any] = {"scheme": "exact"}
    if accept is not None:
        merged.update(dict(accept))
    else:
        if isinstance(settlement.get("network"), str):
            merged["network"] = settlement["network"]
        if isinstance(settlement.get("amount"), str):
            merged["amount"] = settlement["amount"]
    network = _str(settlement.get("network")) or _str(merged.get("network")) or "unknown"
    success = settlement.get("success") is True
    payer = _str(settlement.get("payer"))
    extra: dict[str, Any] = {}
    if nonce is not None:
        extra["nonce"] = nonce
    tx = _str(settlement.get("transaction"))
    if tx:
        extra["tx_hash"] = tx
    payment: dict[str, Any] = {
        "payment_id": f"x402:{network}:{nonce or 'unknown'}",
        "rail": "x402",
        "amount": _amount(merged, _str(settlement.get("amount")) or _str(merged.get("amount"))),
        "status": "settled" if success else "failed",
        "rail_ref": _rail_ref(merged, extra),
    }
    if not success:
        payment["failure_reason"] = (_str(settlement.get("errorReason")) or "settlement_failed")[:128]
    _description(payment, resource)
    value = dict(settlement)
    mapped: dict[str, Any] = {
        "type": "payment.settled" if success else "payment.failed",
        "source": {"adapter": adapter},
        **_attribution(payer),
        "payment": payment,
        "payee": _payee(merged, resource, payee_override),
        "evidence": [
            {"kind": "x402.settlement", "format": "json", "value": value, "sha256": evidence_sha256(value)}
        ],
    }
    if payer is not None and network != "unknown":
        mapped["instrument"] = {"type": "wallet", "wallet": {"network": network, "address": payer}}
    return mapped
