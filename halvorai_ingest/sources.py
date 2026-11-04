"""Register a public Ed25519 key as a HalvorAI source (`POST /v1/sources`).

Used by `init` for a server's own key and by `halvorai_ingest.device` for an app install's key.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx


class SourceRegistrationError(RuntimeError):
    """`POST /v1/sources` did not register the key. `status` is None when there was no answer."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def sources_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/v1/events"):
        base = base[: -len("/v1/events")]
    return f"{base}/v1/sources"


def _request(api_key: str, name: str, public_jwk: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "json": {"name": name, "adapter": "custom", "public_jwk": dict(public_jwk)},
        "headers": {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    }


def _source_id(res: httpx.Response) -> str:
    if res.status_code != 201:
        raise SourceRegistrationError(
            f"POST /v1/sources failed ({res.status_code}): {res.text or res.reason_phrase}",
            res.status_code,
        )
    try:
        body = res.json()
    except ValueError:
        body = None
    source_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(source_id, str) or not source_id.startswith("src_"):
        raise SourceRegistrationError("POST /v1/sources returned no source id", res.status_code)
    return source_id


def post_source(
    http: httpx.Client, endpoint: str, api_key: str, name: str, public_jwk: Mapping[str, Any]
) -> str:
    try:
        res = http.post(sources_url(endpoint), **_request(api_key, name, public_jwk))
    except httpx.HTTPError as exc:
        raise SourceRegistrationError(f"POST /v1/sources failed: {exc}") from exc
    return _source_id(res)


async def post_source_async(
    http: httpx.AsyncClient, endpoint: str, api_key: str, name: str, public_jwk: Mapping[str, Any]
) -> str:
    try:
        res = await http.post(sources_url(endpoint), **_request(api_key, name, public_jwk))
    except httpx.HTTPError as exc:
        raise SourceRegistrationError(f"POST /v1/sources failed: {exc}") from exc
    return _source_id(res)
