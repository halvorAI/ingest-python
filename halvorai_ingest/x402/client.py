"""Observe x402 payment headers on httpx traffic and emit APE events.

Two ways to plug in, both never in the payment path (headers are read, nothing is changed):

- **Transports** (recommended): ``HalvorX402Transport`` and ``AsyncHalvorX402Transport`` sit
  under any ``httpx.Client`` / ``httpx.AsyncClient`` and see every request, including
  ``stream()`` and every HTTP method, with the headers exactly as sent.
- **Wrappers**: ``wrap_httpx`` / ``wrap_httpx_async`` wrap an existing client. Every method that
  goes through ``request`` is observed; ``stream()`` is passed through unobserved, so use a
  transport if you stream.
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

import httpx

from ..client import HalvorClient
from .decode import decode_payment_header
from .map import map_payment_payload, map_payment_required, map_settlement_response


def _header(headers: httpx.Headers, name: str) -> str | None:
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _url_key(url: httpx.URL | str) -> str:
    return str(url).split("#", 1)[0]


class _Observer:
    """Shared by the transports and wrappers. Remembers, per URL, the last challenge and payload."""

    def __init__(self, halvor: HalvorClient) -> None:
        self.halvor = halvor
        self._lock = threading.Lock()
        self._requested: dict[str, str] = {}

    def _report(self, exc: Exception, what: str) -> None:
        self.halvor.notify({"code": "invalid_event", "message": str(exc) or f"{what} failed"})

    def before(self, url: httpx.URL | str, headers: httpx.Headers) -> dict[str, Any] | None:
        """Called with the request headers; returns the decoded payment payload, if any."""
        signature = _header(headers, "PAYMENT-SIGNATURE")
        if not signature:
            return None
        try:
            decoded = decode_payment_header(signature)
        except Exception as exc:
            self._report(exc, "PAYMENT-SIGNATURE decode")
            return None
        if not isinstance(decoded, dict):
            return None
        with self._lock:
            previous = self._requested.pop(_url_key(url), None)
        try:
            mapped = map_payment_payload(decoded, "x402-client", previous_event_id=previous)
            if mapped is not None:
                self.halvor.emit(mapped)
        except Exception as exc:
            self._report(exc, "PAYMENT-SIGNATURE mapping")
        return decoded

    def after(self, url: httpx.URL | str, response: httpx.Response, payload: Mapping[str, Any] | None) -> None:
        if response.status_code == 402:
            required_header = _header(response.headers, "PAYMENT-REQUIRED")
            if required_header:
                try:
                    required = decode_payment_header(required_header)
                    if isinstance(required, dict):
                        mapped = map_payment_required(required, "x402-client")
                        if mapped is not None:
                            event_id = self.halvor.emit(mapped)
                            with self._lock:
                                self._requested[_url_key(url)] = event_id
                except Exception as exc:
                    self._report(exc, "PAYMENT-REQUIRED")

        settlement_header = _header(response.headers, "PAYMENT-RESPONSE")
        if not settlement_header:
            return
        try:
            settlement = decode_payment_header(settlement_header)
            if not isinstance(settlement, dict):
                return
            accept = payload.get("accepted") if payload is not None else None
            inner = payload.get("payload") if payload is not None else None
            auth = inner.get("authorization") if isinstance(inner, Mapping) else None
            nonce = auth.get("nonce") if isinstance(auth, Mapping) else None
            resource = payload.get("resource") if payload is not None else None
            mapped = map_settlement_response(
                settlement,
                accept=accept if isinstance(accept, Mapping) else None,
                nonce=nonce if isinstance(nonce, str) else None,
                resource=resource if isinstance(resource, Mapping) else None,
                adapter="x402-client",
            )
            if mapped is not None:
                self.halvor.emit(mapped)
        except Exception as exc:
            self._report(exc, "PAYMENT-RESPONSE")


class HalvorX402Transport(httpx.BaseTransport):
    """Sync transport: ``httpx.Client(transport=HalvorX402Transport(halvor))``."""

    def __init__(self, halvor: HalvorClient, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner or httpx.HTTPTransport()
        self._observer = _Observer(halvor)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = self._observer.before(request.url, request.headers)
        response = self._inner.handle_request(request)
        self._observer.after(request.url, response, payload)
        return response

    def close(self) -> None:
        self._inner.close()


class AsyncHalvorX402Transport(httpx.AsyncBaseTransport):
    """Async transport: ``httpx.AsyncClient(transport=AsyncHalvorX402Transport(halvor))``."""

    def __init__(self, halvor: HalvorClient, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._observer = _Observer(halvor)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = self._observer.before(request.url, request.headers)
        response = await self._inner.handle_async_request(request)
        self._observer.after(request.url, response, payload)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class HalvorHttpxClient:
    """Delegating wrapper around ``httpx.Client`` that emits x402 APE events."""

    def __init__(self, inner: httpx.Client, halvor: HalvorClient) -> None:
        self._inner = inner
        self._halvor = halvor
        self._observer = _Observer(halvor)

    def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        payload = self._observer.before(str(url), httpx.Headers(kwargs.get("headers")))
        response = self._inner.request(method, url, **kwargs)
        self._observer.after(str(url), response, payload)
        return response

    def get(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("HEAD", url, **kwargs)

    def options(self, url: Any, **kwargs: Any) -> httpx.Response:
        return self.request("OPTIONS", url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Everything else (stream, headers, cookies, …) goes straight to the inner client.
        return getattr(self._inner, name)

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> HalvorHttpxClient:
        self._inner.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._inner.__exit__(*args)


class AsyncHalvorHttpxClient:
    """Delegating wrapper around ``httpx.AsyncClient`` that emits x402 APE events."""

    def __init__(self, inner: httpx.AsyncClient, halvor: HalvorClient) -> None:
        self._inner = inner
        self._halvor = halvor
        self._observer = _Observer(halvor)

    async def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        payload = self._observer.before(str(url), httpx.Headers(kwargs.get("headers")))
        response = await self._inner.request(method, url, **kwargs)
        self._observer.after(str(url), response, payload)
        return response

    async def get(self, url: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("OPTIONS", url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> AsyncHalvorHttpxClient:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._inner.__aexit__(*args)


def wrap_httpx(client: httpx.Client, halvor: HalvorClient) -> HalvorHttpxClient:
    """Wrap a sync client (typically a payment-aware one) so HalvorAI observes x402 headers."""
    return HalvorHttpxClient(client, halvor)


def wrap_httpx_async(client: httpx.AsyncClient, halvor: HalvorClient) -> AsyncHalvorHttpxClient:
    """Wrap an async client so HalvorAI observes x402 headers."""
    return AsyncHalvorHttpxClient(client, halvor)
