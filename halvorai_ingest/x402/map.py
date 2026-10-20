"""Map x402 v2 HTTP payloads to partial APE events (parity with TS adapter)."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse

from ..context import current_context
from ..hash import evidence_sha256


def _host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def _attribution(payer_wallet: str | None = None) -> dict[str, Any]:
    ctx = current_context()
    agent_id = (ctx or {}).get("agent", {}).get("id") if ctx else None
    owner = (ctx or {}).get("owner") if ctx else None
    return {
        "agent": {"id": agent_id or payer_wallet or "unattributed"},
        "owner": {
            "id": (owner or {}).get("id") if isinstance(owner, Mapping) else "unattributed",
            "type": (owner or {}).get("type") if isinstance(owner, Mapping) else "business",
        },
    }


def _asset(accept: Mapping[str, Any]) -> dict[str, Any]:
    raw_extra = accept.get("extra")
    extra: Mapping[str, Any] = raw_extra if isinstance(raw_extra, Mapping) else {}
    return {
        "network": accept.get("network") or "eip155:84532",
        "address": accept.get("asset") or "0x0000000000000000000000000000000000000000",
        "symbol": extra.get("name") or "USDC",
        "decimals": extra.get("decimals") if isinstance(extra.get("decimals"), int) else 6,
    }


def _amount(accept: Mapping[str, Any], override: str | None = None) -> dict[str, Any]:
    return {
        "value": override or accept.get("amount") or "0",
        "unit": "atomic",
        "asset": _asset(accept),
    }


def map_payment_required(
    required: Mapping[str, Any],
    adapter: str = "x402-client",
) -> dict[str, Any] | None:
    accepts = required.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        return None
    accept = accepts[0]
    if not isinstance(accept, Mapping):
        return None
    raw_resource = required.get("resource")
    resource: Mapping[str, Any] = raw_resource if isinstance(raw_resource, Mapping) else {}
    attr = _attribution()
    network = accept.get("network") or "eip155:84532"
    payment_id = f"x402:{network}:challenge"
    payee: dict[str, Any] = {"type": "api_provider"}
    name = resource.get("serviceName") or resource.get("description") or _host(
        resource.get("url") if isinstance(resource.get("url"), str) else None
    )
    if isinstance(name, str):
        payee["name"] = name[:256]
    url = resource.get("url")
    if isinstance(url, str):
        payee["resource_url"] = url
    if isinstance(accept.get("payTo"), str) and isinstance(accept.get("network"), str):
        payee["wallet"] = {"network": accept["network"], "address": accept["payTo"]}
    return {
        "type": "payment.requested",
        "source": {"adapter": adapter},
        **attr,
        "payment": {
            "payment_id": payment_id,
            "rail": "x402",
            "amount": _amount(accept),
            "status": "requested",
            "rail_ref": {
                "scheme": accept.get("scheme") or "exact",
                "network": network,
            },
        },
        "payee": payee,
        "evidence": [],
    }


def map_payment_payload(
    payload: Mapping[str, Any],
    adapter: str = "x402-client",
) -> dict[str, Any] | None:
    accepted = payload.get("accepted")
    if not isinstance(accepted, Mapping):
        return None
    auth = {}
    inner = payload.get("payload")
    if isinstance(inner, Mapping) and isinstance(inner.get("authorization"), Mapping):
        auth = inner["authorization"]
    attr = _attribution(auth.get("from") if isinstance(auth.get("from"), str) else None)
    network = accepted.get("network") or "eip155:84532"
    nonce = auth.get("nonce") if isinstance(auth.get("nonce"), str) else "unknown"
    value = dict(payload)
    return {
        "type": "payment.authorized",
        "source": {"adapter": adapter},
        **attr,
        "payment": {
            "payment_id": f"x402:{network}:{nonce}",
            "rail": "x402",
            "amount": _amount(accepted, auth.get("value") if isinstance(auth.get("value"), str) else None),
            "status": "authorized",
            "rail_ref": {
                "scheme": accepted.get("scheme") or "exact",
                "network": network,
                "nonce": nonce,
            },
        },
        "payee": {
            "type": "wallet",
            "wallet": {
                "network": network,
                "address": auth.get("to") or accepted.get("payTo") or "0x0",
            },
        },
        "evidence": [
            {
                "kind": "x402.payment_payload",
                "format": "json",
                "value": value,
                "sha256": evidence_sha256(value),
            }
        ],
    }


def map_settlement_response(
    settlement: Mapping[str, Any],
    *,
    accept: Mapping[str, Any] | None = None,
    nonce: str | None = None,
    resource: Mapping[str, Any] | None = None,
    adapter: str = "x402-client",
) -> dict[str, Any] | None:
    success = settlement.get("success") is True
    network = settlement.get("network") or (accept or {}).get("network") or "eip155:84532"
    tx = settlement.get("transaction") if isinstance(settlement.get("transaction"), str) else "unknown"
    attr = _attribution(
        settlement.get("payer") if isinstance(settlement.get("payer"), str) else None
    )
    accept_m = accept or {}
    amount_override = (
        settlement.get("amount") if isinstance(settlement.get("amount"), str) else None
    )
    payment: dict[str, Any] = {
        "payment_id": f"x402:{network}:{tx}",
        "rail": "x402",
        "amount": _amount(accept_m, amount_override),
        "status": "settled" if success else "failed",
        "rail_ref": {
            "scheme": accept_m.get("scheme") or "exact",
            "network": network,
            "tx_hash": tx,
        },
    }
    if nonce:
        payment["rail_ref"]["nonce"] = nonce
    if not success:
        payment["failure_reason"] = settlement.get("errorReason") or "settlement_failed"
    payee: dict[str, Any] = {"type": "api_provider"}
    if resource and isinstance(resource.get("url"), str):
        payee["resource_url"] = resource["url"]
        host = _host(resource["url"])
        if host:
            payee["name"] = host
    value = dict(settlement)
    return {
        "type": "payment.settled" if success else "payment.failed",
        "source": {"adapter": adapter},
        **attr,
        "payment": payment,
        "payee": payee,
        "evidence": [
            {
                "kind": "x402.settlement",
                "format": "json",
                "value": value,
                "sha256": evidence_sha256(value),
            }
        ],
    }
