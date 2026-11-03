"""Server-side helpers for apps whose installs sign their own events (`halvorai_ingest_flutter`).

The app never holds your API key. Your backend:

- registers each install's public key (`register_device_source`) and answers the app's
  provisioning request with `{"source_id": "src_…"}`;
- checks and forwards each signed batch (`forward_envelopes`), answering with HalvorAI's response
  unchanged.

Both come in sync and async forms; use the async ones in FastAPI and other async frameworks.
The status codes tell the app what to do: 401 or 403 (your own session check) means try later,
400, 413 and 422 are permanent, and 502 means HalvorAI could not be reached.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .client import API_KEY_PATTERN, default_endpoint
from .sources import SourceRegistrationError, post_source, post_source_async

DEFAULT_MAX_ENVELOPES = 100
DEFAULT_MAX_BYTES = 1_000_000

_BASE64URL_32_BYTES = re.compile(r"^[A-Za-z0-9_-]{43}$")


class DeviceRequestError(Exception):
    """A request the app sent that your backend must answer with `status`.

    `body()` is HalvorAI's error format, so the Flutter SDK reports `code` as `serverCode`.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def body(self) -> bytes:
        return json.dumps({"error": {"code": self.code, "message": self.message}}).encode("utf-8")


@dataclass(frozen=True)
class ForwardResult:
    """What to answer the app: HalvorAI's response unchanged, or why the batch was not sent."""

    status: int
    body: bytes
    content_type: str


@dataclass(frozen=True)
class _Config:
    api_key: str
    environment: str
    endpoint: str


def _config(api_key: str | None, endpoint: str | None) -> _Config:
    """A misconfigured backend is a programming error, so this raises ValueError."""
    key = api_key or os.environ.get("HALVOR_API_KEY")
    if not key or not API_KEY_PATTERN.match(key):
        raise ValueError("HalvorAI api key is missing or malformed (expected hk_test_… or hk_live_…)")
    environment = "live" if key.startswith("hk_live_") else "test"
    base = endpoint or os.environ.get("HALVOR_ENDPOINT") or default_endpoint(key, environment)
    base = base.rstrip("/")
    if base.endswith("/v1/events"):
        base = base[: -len("/v1/events")]
    return _Config(key, environment, base)


def public_device_jwk(jwk: object) -> dict[str, str]:
    """The public half of an Ed25519 key, reduced to `kty`, `crv` and `x`.

    A key with `d` is refused: the app sent its private key, which must never leave the device.
    """
    if not isinstance(jwk, dict):
        raise DeviceRequestError(400, "invalid_request", "public_jwk must be a JSON object")
    if "d" in jwk:
        raise DeviceRequestError(
            400, "invalid_request", "public_jwk contains a private key (d); only the public half may be sent"
        )
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise DeviceRequestError(400, "invalid_request", "public_jwk must be an Ed25519 key")
    x = jwk.get("x")
    if not isinstance(x, str) or not _BASE64URL_32_BYTES.match(x):
        raise DeviceRequestError(400, "invalid_request", "public_jwk.x must be 32 bytes, base64url")
    return {"kty": "OKP", "crv": "Ed25519", "x": x}


def _name(name: str | None) -> str:
    return ((name or "").strip() or "app install")[:100]


def register_device_source(
    public_jwk: object,
    *,
    name: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Register an app install's public key and return the `src_` id the app signs with.

    Call it from the route the app's provisioning request reaches, after checking the user is
    signed in. Raises `DeviceRequestError`: 400 for a key the app should not have sent, 502 when
    HalvorAI does not register it (the app retries).
    """
    config = _config(api_key, endpoint)
    jwk = public_device_jwk(public_jwk)
    try:
        with httpx.Client(timeout=30.0, transport=transport) as http:
            return post_source(http, config.endpoint, config.api_key, _name(name), jwk)
    except SourceRegistrationError as exc:
        raise DeviceRequestError(502, "upstream_error", str(exc)) from exc


async def register_device_source_async(
    public_jwk: object,
    *,
    name: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """`register_device_source` for async frameworks."""
    config = _config(api_key, endpoint)
    jwk = public_device_jwk(public_jwk)
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as http:
            return await post_source_async(http, config.endpoint, config.api_key, _name(name), jwk)
    except SourceRegistrationError as exc:
        raise DeviceRequestError(502, "upstream_error", str(exc)) from exc


def _check_batch(body: bytes, expected_owner_id: str, environment: str, max_envelopes: int, max_bytes: int) -> None:
    if len(body) > max_bytes:
        raise DeviceRequestError(413, "payload_too_large", f"body is over {max_bytes} bytes")
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DeviceRequestError(400, "invalid_request", "body is not UTF-8 JSON") from None
    events = parsed.get("events") if isinstance(parsed, dict) else None
    if not isinstance(events, list) or not events:
        raise DeviceRequestError(400, "invalid_request", 'body must be {"events":[…]} with at least one envelope')
    if len(events) > max_envelopes:
        raise DeviceRequestError(413, "payload_too_large", f"more than {max_envelopes} envelopes")
    for i, envelope in enumerate(events):
        event = envelope.get("event") if isinstance(envelope, dict) else None
        if not isinstance(event, dict) or not isinstance(envelope.get("sig"), str):
            raise DeviceRequestError(400, "invalid_request", f'events[{i}] must be {{"event":{{…}},"sig":"…"}}')
        owner = event.get("owner")
        if not isinstance(owner, dict) or owner.get("id") != expected_owner_id:
            raise DeviceRequestError(422, "owner_mismatch", f"events[{i}].event.owner.id is not the signed-in user")
        if event.get("environment") != environment:
            raise DeviceRequestError(
                422,
                "environment_mismatch",
                f"events[{i}].event.environment must be {environment}, the API key's environment",
            )


def _prepare(
    body: bytes | str,
    expected_owner_id: str,
    api_key: str | None,
    endpoint: str | None,
    max_envelopes: int,
    max_bytes: int,
) -> tuple[_Config, bytes, ForwardResult | None]:
    config = _config(api_key, endpoint)
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    try:
        _check_batch(raw, expected_owner_id, config.environment, max_envelopes, max_bytes)
    except DeviceRequestError as exc:
        return config, raw, _error(exc)
    return config, raw, None


def _error(exc: DeviceRequestError) -> ForwardResult:
    return ForwardResult(exc.status, exc.body(), "application/json")


def _headers(config: _Config) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _result(res: httpx.Response) -> ForwardResult:
    return ForwardResult(res.status_code, res.content, res.headers.get("content-type", "application/json"))


def _unreachable(exc: Exception) -> ForwardResult:
    return _error(DeviceRequestError(502, "upstream_error", f"HalvorAI could not be reached: {exc}"))


def forward_envelopes(
    body: bytes | str,
    *,
    expected_owner_id: str,
    api_key: str | None = None,
    endpoint: str | None = None,
    max_envelopes: int = DEFAULT_MAX_ENVELOPES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    transport: httpx.BaseTransport | None = None,
) -> ForwardResult:
    """Check a batch an app install sent and pass it to `POST /v1/events` byte for byte.

    Returns HalvorAI's status and body unchanged; the app reads its per-event results from them.
    The body is parsed only to check it: re-serializing could change the bytes the device signed.

    Refused without calling HalvorAI, in HalvorAI's error format: 400 `invalid_request` for a body
    that is not `{"events":[{"event":…,"sig":…}]}`, 413 `payload_too_large` above `max_envelopes`
    or `max_bytes` (the app splits the batch), 422 `owner_mismatch` when an event's `owner.id` is
    not `expected_owner_id`, 422 `environment_mismatch` when its `environment` is not the key's.
    502 when HalvorAI cannot be reached. Answer 401 or 403 yourself for a session that is not valid.
    """
    config, raw, refused = _prepare(body, expected_owner_id, api_key, endpoint, max_envelopes, max_bytes)
    if refused is not None:
        return refused
    try:
        with httpx.Client(timeout=30.0, transport=transport) as http:
            res = http.post(f"{config.endpoint}/v1/events", content=raw, headers=_headers(config))
    except httpx.HTTPError as exc:
        return _unreachable(exc)
    return _result(res)


async def forward_envelopes_async(
    body: bytes | str,
    *,
    expected_owner_id: str,
    api_key: str | None = None,
    endpoint: str | None = None,
    max_envelopes: int = DEFAULT_MAX_ENVELOPES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ForwardResult:
    """`forward_envelopes` for async frameworks."""
    config, raw, refused = _prepare(body, expected_owner_id, api_key, endpoint, max_envelopes, max_bytes)
    if refused is not None:
        return refused
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as http:
            res = await http.post(f"{config.endpoint}/v1/events", content=raw, headers=_headers(config))
    except httpx.HTTPError as exc:
        return _unreachable(exc)
    return _result(res)


__all__ = [
    "DeviceRequestError",
    "ForwardResult",
    "forward_envelopes",
    "forward_envelopes_async",
    "public_device_jwk",
    "register_device_source",
    "register_device_source_async",
]
