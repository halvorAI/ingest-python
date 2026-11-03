"""Server-side helpers for app installs that sign their own events (halvorai_ingest_flutter)."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from halvorai_ingest import generate_source_key
from halvorai_ingest.device import (
    DeviceRequestError,
    forward_envelopes,
    forward_envelopes_async,
    register_device_source,
    register_device_source_async,
)

API_KEY = "hk_live_" + "a" * 32
ENDPOINT = "https://api.example"
PUBLIC_JWK = {k: v for k, v in generate_source_key().items() if k not in ("d", "kid")}


class FakeApi:
    """A fake HalvorAI that records requests and answers with `respond`."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def accepted(request: httpx.Request) -> httpx.Response:
    events = json.loads(request.content)["events"]
    return httpx.Response(200, json={"results": [{"id": e["event"]["id"], "status": "accepted"} for e in events]})


def envelope(**over: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": "evt_01JA8Q4K3T9V2C7M5N6P8R0S1W",
        "environment": "live",
        "owner": {"id": "own_u_1", "type": "person"},
    }
    event.update(over)
    return {"event": event, "sig": "eyJhbGciOiJFZERTQSJ9..c2ln"}


# register_device_source


def test_registers_only_the_public_half_and_returns_the_source_id() -> None:
    api = FakeApi(lambda _: httpx.Response(201, json={"id": "src_device_1"}))
    source_id = register_device_source(
        {**PUBLIC_JWK, "kid": "src_pending", "alg": "EdDSA"},
        name="iOS app",
        api_key=API_KEY,
        endpoint=ENDPOINT,
        transport=api.transport,
    )
    assert source_id == "src_device_1"
    request = api.requests[0]
    assert str(request.url) == "https://api.example/v1/sources"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {
        "name": "iOS app",
        "adapter": "custom",
        "public_jwk": {"kty": "OKP", "crv": "Ed25519", "x": PUBLIC_JWK["x"]},
    }


@pytest.mark.parametrize(
    ("jwk", "message"),
    [
        ({**PUBLIC_JWK, "d": "secret"}, "private key"),
        ({**PUBLIC_JWK, "crv": "P-256"}, "Ed25519"),
        ({**PUBLIC_JWK, "x": "abc"}, "32 bytes"),
        ("key", "JSON object"),
    ],
)
def test_refuses_a_bad_key_with_400_and_never_calls_halvorai(jwk: object, message: str) -> None:
    api = FakeApi(lambda _: httpx.Response(201, json={"id": "src_x"}))
    with pytest.raises(DeviceRequestError) as info:
        register_device_source(jwk, api_key=API_KEY, endpoint=ENDPOINT, transport=api.transport)
    assert info.value.status == 400
    assert message in info.value.message
    assert api.requests == []


def test_answers_502_when_halvorai_does_not_register_the_key() -> None:
    api = FakeApi(lambda _: httpx.Response(503, text="down"))
    with pytest.raises(DeviceRequestError) as info:
        register_device_source(PUBLIC_JWK, api_key=API_KEY, endpoint=ENDPOINT, transport=api.transport)
    assert info.value.status == 502
    assert info.value.code == "upstream_error"
    assert json.loads(info.value.body())["error"]["code"] == "upstream_error"


def test_refuses_to_start_without_a_well_formed_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HALVOR_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api key"):
        register_device_source(PUBLIC_JWK, api_key="hk_nope")


async def test_async_registration_works_the_same() -> None:
    api = FakeApi(lambda _: httpx.Response(201, json={"id": "src_device_2"}))
    source_id = await register_device_source_async(
        PUBLIC_JWK, api_key=API_KEY, endpoint=ENDPOINT, transport=api.transport
    )
    assert source_id == "src_device_2"


# forward_envelopes


def test_passes_the_body_byte_for_byte_and_returns_the_answer_unchanged() -> None:
    api = FakeApi(accepted)
    # Unusual spacing and key order: re-serializing would change these bytes.
    raw = '{ "events" : [ ' + json.dumps(envelope()) + ' ] , "extra": 1 }'
    result = forward_envelopes(
        raw, expected_owner_id="own_u_1", api_key=API_KEY, endpoint=ENDPOINT, transport=api.transport
    )
    request = api.requests[0]
    assert str(request.url) == "https://api.example/v1/events"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.content == raw.encode("utf-8")
    assert result.status == 200
    assert json.loads(result.body) == {"results": [{"id": "evt_01JA8Q4K3T9V2C7M5N6P8R0S1W", "status": "accepted"}]}


def test_returns_halvorai_errors_unchanged() -> None:
    api = FakeApi(lambda _: httpx.Response(503, content=b'{"error":{"code":"unavailable"}}'))
    result = forward_envelopes(
        json.dumps({"events": [envelope()]}),
        expected_owner_id="own_u_1",
        api_key=API_KEY,
        endpoint=ENDPOINT,
        transport=api.transport,
    )
    assert result.status == 503
    assert result.body == b'{"error":{"code":"unavailable"}}'


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"events": [envelope(owner={"id": "own_someone_else", "type": "person"})]}, 422, "owner_mismatch"),
        ({"events": [envelope(owner=None)]}, 422, "owner_mismatch"),
        ({"events": [envelope(environment="test")]}, 422, "environment_mismatch"),
        ({"events": []}, 400, "invalid_request"),
        ({"events": [{"event": envelope()["event"]}]}, 400, "invalid_request"),
        ({"events": [envelope() for _ in range(101)]}, 413, "payload_too_large"),
    ],
)
def test_refuses_a_bad_batch_without_calling_halvorai(body: Any, status: int, code: str) -> None:
    api = FakeApi(accepted)
    result = forward_envelopes(
        json.dumps(body), expected_owner_id="own_u_1", api_key=API_KEY, endpoint=ENDPOINT, transport=api.transport
    )
    assert result.status == status
    assert json.loads(result.body)["error"]["code"] == code
    assert api.requests == []


def test_refuses_a_body_that_is_not_json_or_too_large() -> None:
    api = FakeApi(accepted)
    options: dict[str, Any] = {
        "expected_owner_id": "own_u_1",
        "api_key": API_KEY,
        "endpoint": ENDPOINT,
        "transport": api.transport,
    }
    assert forward_envelopes(b"not json", **options).status == 400
    assert forward_envelopes(b"\xff\xfe", **options).status == 400
    assert forward_envelopes(b"x" * 1_000_001, **options).status == 413
    assert api.requests == []


def test_answers_502_when_halvorai_cannot_be_reached() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = forward_envelopes(
        json.dumps({"events": [envelope()]}),
        expected_owner_id="own_u_1",
        api_key=API_KEY,
        endpoint=ENDPOINT,
        transport=httpx.MockTransport(fail),
    )
    assert result.status == 502
    assert b"upstream_error" in result.body


async def test_async_forwarding_works_the_same() -> None:
    api = FakeApi(accepted)
    raw = json.dumps({"events": [envelope()]}).encode("utf-8")
    result = await forward_envelopes_async(
        raw, expected_owner_id="own_u_1", api_key=API_KEY, endpoint=ENDPOINT, transport=api.transport
    )
    assert result.status == 200
    assert api.requests[0].content == raw
