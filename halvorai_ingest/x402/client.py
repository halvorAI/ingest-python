"""Wrap an httpx client so HalvorAI observes x402 payment headers."""

from __future__ import annotations

from typing import Any

import httpx

from ..client import HalvorClient
from .decode import decode_payment_header
from .map import map_payment_payload, map_payment_required, map_settlement_response


def _header(headers: httpx.Headers, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


class HalvorHttpxClient:
    """Delegating wrapper around httpx.Client that emits x402 APE events."""

    def __init__(self, inner: httpx.Client, halvor: HalvorClient) -> None:
        self._inner = inner
        self._halvor = halvor

    def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        headers = httpx.Headers(kwargs.get("headers"))
        payload: dict[str, Any] | None = None
        sig = _header(headers, "PAYMENT-SIGNATURE")
        if sig:
            try:
                decoded = decode_payment_header(sig)
                if isinstance(decoded, dict):
                    payload = decoded
                    mapped = map_payment_payload(decoded, "x402-client")
                    if mapped is not None:
                        self._halvor.emit(mapped)
            except Exception as exc:
                self._halvor.notify(
                    {
                        "code": "invalid_event",
                        "message": str(exc) or "PAYMENT-SIGNATURE decode failed",
                    }
                )

        response = self._inner.request(method, url, **kwargs)

        if response.status_code == 402:
            required_h = _header(response.headers, "PAYMENT-REQUIRED")
            if required_h:
                try:
                    required = decode_payment_header(required_h)
                    if isinstance(required, dict):
                        mapped = map_payment_required(required, "x402-client")
                        if mapped is not None:
                            self._halvor.emit(mapped)
                except Exception as exc:
                    self._halvor.notify(
                        {
                            "code": "invalid_event",
                            "message": str(exc) or "PAYMENT-REQUIRED decode failed",
                        }
                    )

        settlement_h = _header(response.headers, "PAYMENT-RESPONSE")
        if settlement_h:
            try:
                settlement = decode_payment_header(settlement_h)
                if isinstance(settlement, dict):
                    accept = payload.get("accepted") if payload is not None else None
                    auth = None
                    if payload is not None:
                        inner = payload.get("payload")
                        if isinstance(inner, dict):
                            auth = inner.get("authorization")
                    nonce = (
                        auth.get("nonce")
                        if isinstance(auth, dict) and isinstance(auth.get("nonce"), str)
                        else None
                    )
                    resource = payload.get("resource") if payload is not None else None
                    mapped = map_settlement_response(
                        settlement,
                        accept=accept if isinstance(accept, dict) else None,
                        nonce=nonce,
                        resource=resource if isinstance(resource, dict) else None,
                        adapter="x402-client",
                    )
                    if mapped is not None:
                        self._halvor.emit(mapped)
            except Exception as exc:
                self._halvor.notify(
                    {
                        "code": "invalid_event",
                        "message": str(exc) or "PAYMENT-RESPONSE decode failed",
                    }
                )

        return response

    def get(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> HalvorHttpxClient:
        self._inner.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._inner.__exit__(*args)


def wrap_httpx(client: httpx.Client, halvor: HalvorClient) -> HalvorHttpxClient:
    """
    Return a wrapper around `client` that emits APE events for x402 headers.
    Composition: wrap_httpx(payment_aware_client, halvor).
    """
    return HalvorHttpxClient(client, halvor)
