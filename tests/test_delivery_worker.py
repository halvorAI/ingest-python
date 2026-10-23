"""Background delivery, retries, spool and config checks (PY-1, 2, 3, 8, 9, 10, 11, 12)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from halvorai_ingest import HalvorClient, agent_context, generate_source_key
from halvorai_ingest.spool import Spool

API_KEY = "hk_test_" + "d" * 32
PAYMENT = {
    "payment_id": "pi_1",
    "rail": "stripe",
    "amount": {"value": "100", "unit": "minor", "currency": "USD"},
    "status": "settled",
    "rail_ref": {"payment_intent": "pi_1"},
}
EVENT: dict[str, Any] = {"type": "payment.settled", "payment": PAYMENT, "payee": {"type": "merchant", "name": "Shop"}}


def accept(request: httpx.Request) -> httpx.Response:
    ids = [e["event"]["id"] for e in json.loads(request.content)["events"]]
    return httpx.Response(202, json={"results": [{"id": i, "status": "accepted"} for i in ids]})


def http_client(
    handler: Callable[[httpx.Request], httpx.Response], tmp_path: Path | None = None, **kw: Any
) -> tuple[HalvorClient, list[dict[str, Any]], list[list[str]]]:
    errors: list[dict[str, Any]] = []
    sent: list[list[str]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append([e["event"]["id"] for e in json.loads(request.content)["events"]])
        return handler(request)

    options: dict[str, Any] = {
        "api_key": API_KEY,
        "source_key": generate_source_key(),
        "endpoint": "http://api.test",
        "transport": httpx.MockTransport(recording),
        "on_error": errors.append,
        "retry_delays": (),
        "exit_hook": False,
    }
    if tmp_path is not None:
        options["spool_dir"] = str(tmp_path)
    options.update(kw)
    return HalvorClient(**options), errors, sent


def pending(tmp_path: Path) -> list[str]:
    return sorted(n for n in os.listdir(tmp_path) if n.endswith(".jsonl"))


def dead(tmp_path: Path) -> list[str]:
    folder = tmp_path / "dead"
    if not folder.is_dir():
        return []
    return [line for f in os.listdir(folder) for line in (folder / f).read_text().splitlines() if line]


# PY-1
def test_emit_never_waits_for_the_network() -> None:
    slow = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        slow.wait(5)
        return accept(request)

    client, _, _ = http_client(handler, batch_size=10)
    started = time.monotonic()
    for _ in range(10):
        client.emit(EVENT)
    assert time.monotonic() - started < 0.5
    slow.set()
    assert client.flush(timeout=5) == {"drained": True}
    assert client.stats().accepted == 10
    client.close()


# PY-2
def test_retries_503_then_delivers() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] <= 2 else accept(request)

    client, _, _ = http_client(handler, retry_delays=(0.01, 0.01, 0.01))
    client.emit(EVENT)
    client.flush(timeout=5)
    assert (client.stats().retries, client.stats().accepted, client.stats().dropped) == (2, 1, 0)
    client.close()


def test_network_errors_spool_the_events_instead_of_dropping_them(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client, _, _ = http_client(handler, tmp_path, retry_delays=(0.01,))
    client.emit(EVENT)
    client.flush(timeout=5, replay_spool=False)
    stats = client.stats()
    assert (stats.spooled, stats.dropped, stats.rejected) == (1, 0, 0)
    assert len(pending(tmp_path)) == 1
    client.close()


def test_spooled_events_are_replayed_by_the_next_process(tmp_path: Path) -> None:
    down, _, _ = http_client(lambda r: httpx.Response(503), tmp_path)
    event_id = down.emit(EVENT)
    down.flush(timeout=5, replay_spool=False)
    down.close()

    up, _, sent = http_client(accept, tmp_path)
    up.flush(timeout=5)
    assert [event_id] in sent
    assert pending(tmp_path) == []
    assert up.stats().replayed == 1
    up.close()


# PY-9
def test_5xx_is_not_counted_as_rejected() -> None:
    client, errors, _ = http_client(lambda r: httpx.Response(503))
    client.emit(EVENT)
    client.flush(timeout=5)
    assert client.stats().rejected == 0
    assert client.stats().dropped == 1
    assert errors[-1]["code"] == "delivery_failed"
    client.close()


def test_fatal_errors_carry_the_server_code_and_request_id(tmp_path: Path) -> None:
    body = {"error": {"code": "unknown_source", "message": "no such source", "request_id": "req_9"}}
    client, errors, sent = http_client(lambda r: httpx.Response(401, json=body), tmp_path, retry_delays=(0.01,))
    client.emit(EVENT)
    client.flush(timeout=5)
    assert len(sent) == 1  # never retried
    assert errors[-1] == {
        "code": "unauthorized",
        "message": "no such source",
        "serverCode": "unknown_source",
        "requestId": "req_9",
        "eventId": sent[0][0],
    }
    assert len(dead(tmp_path)) == 1 and pending(tmp_path) == []
    client.close()


def test_a_rejected_spool_file_does_not_block_later_files(tmp_path: Path) -> None:
    down, _, _ = http_client(lambda r: httpx.Response(503), tmp_path)
    bad = down.emit(EVENT)
    down.flush(timeout=5, replay_spool=False)
    good = down.emit(EVENT)
    down.flush(timeout=5, replay_spool=False)
    down.close()
    assert len(pending(tmp_path)) == 2

    def handler(request: httpx.Request) -> httpx.Response:
        ids = [e["event"]["id"] for e in json.loads(request.content)["events"]]
        return httpx.Response(422, json={"error": {"code": "invalid_event"}}) if bad in ids else accept(request)

    up, _, sent = http_client(handler, tmp_path)
    up.flush(timeout=5)
    assert [good] in sent
    assert pending(tmp_path) == []
    assert (up.stats().dropped, up.stats().replayed) == (1, 1)
    up.flush(timeout=5)
    assert up.stats().dropped == 1
    up.close()


def test_a_single_oversized_event_is_dropped_not_spooled(tmp_path: Path) -> None:
    client, errors, _ = http_client(lambda r: httpx.Response(413), tmp_path)
    client.emit(EVENT)
    client.flush(timeout=5)
    assert pending(tmp_path) == []
    assert client.stats().dropped == 1
    assert errors[-1]["serverCode"] == "payload_too_large"
    client.close()


def test_a_batch_that_is_too_large_is_split() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413) if len(json.loads(request.content)["events"]) > 2 else accept(request)

    client, _, sent = http_client(handler)
    for _ in range(5):
        client.emit(EVENT)
    client.flush(timeout=5)
    assert client.stats().accepted == 5
    assert max(len(s) for s in sent if len(s) <= 2) <= 2
    client.close()


def test_spool_evicts_the_oldest_files_beyond_its_limit(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1)
    spool.write([{"event": {"id": "evt_a"}, "sig": "s"}])
    evicted = spool.write([{"event": {"id": "evt_b"}, "sig": "s"}])
    assert evicted.files >= 1 and evicted.events >= 1


# PY-3
def test_sends_in_the_background_without_flush() -> None:
    client, _, sent = http_client(accept, flush_interval=0.05)
    client.emit(EVENT)
    deadline = time.monotonic() + 2
    while not sent and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sent
    client.close()


def test_context_manager_closes_and_sends() -> None:
    _, _, sent_holder = http_client(accept)
    with http_client(accept)[0] as client:
        client.emit(EVENT)
    assert client.stats().accepted == 1
    assert client.emit(EVENT) and client.stats().dropped_closed == 1
    del sent_holder


# PY-10
@pytest.mark.parametrize(
    "key",
    [
        {k: v for k, v in generate_source_key().items() if k != "d"},  # public only
        {**generate_source_key(), "crv": "P-256", "kty": "EC"},
        {k: v for k, v in generate_source_key().items() if k != "kid"},
    ],
)
def test_bad_source_keys_disable_the_client_once(key: dict[str, Any]) -> None:
    errors: list[dict[str, Any]] = []
    client = HalvorClient(api_key=API_KEY, source_key=key, on_error=errors.append, exit_hook=False)
    client.emit(EVENT)
    client.emit(EVENT)
    assert [e["code"] for e in errors] == ["config"]
    assert client.stats().dropped_disabled == 2


def test_malformed_api_key_disables_the_client() -> None:
    errors: list[dict[str, Any]] = []
    client = HalvorClient(api_key="sk_live_nope", source_key=generate_source_key(), on_error=errors.append, exit_hook=False)
    client.emit(EVENT)
    assert errors[0]["code"] == "config"
    assert client.stats().dropped_disabled == 1


# PY-12
def test_stats_returns_a_copy() -> None:
    client, _, _ = http_client(accept)
    snapshot = client.stats()
    snapshot.accepted = 99
    assert client.stats().accepted == 0
    client.close()


def test_counters_are_exact_under_concurrent_emits() -> None:
    received: list[int] = []
    client = HalvorClient(
        source_key=generate_source_key(), sink=lambda b: received.append(len(b)), exit_hook=False
    )

    def worker() -> None:
        for _ in range(200):
            client.emit(EVENT)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    client.flush(timeout=10)
    assert client.stats().emitted == 800
    assert sum(received) == client.stats().accepted == 800
    client.close()


# PY-8
def test_json_evidence_is_redacted_and_rehashed() -> None:
    sent: list[dict[str, Any]] = []
    client = HalvorClient(source_key=generate_source_key(), sink=sent.extend, exit_hook=False)
    event = json.loads(json.dumps(EVENT))
    event["evidence"] = [
        {"kind": "stripe.object", "format": "json", "value": {"note": "card 4242 4242 4242 4242"}, "sha256": "sha256:" + "0" * 64}
    ]
    client.emit(event)
    client.flush(timeout=5)
    evidence = sent[0]["event"]["evidence"][0]
    assert "4242 4242 4242 4242" not in json.dumps(evidence)
    assert evidence["sha256"] != "sha256:" + "0" * 64
    client.close()


# PY-11
def test_unknown_instruction_keys_are_dropped_and_event_level_replaces_context() -> None:
    sent: list[dict[str, Any]] = []
    client = HalvorClient(source_key=generate_source_key(), sink=sent.extend, exit_hook=False)
    with agent_context(instruction={"id": "ctx", "text": "from context", "channel": "chat"}):
        client.emit({**EVENT, "instruction": {"id": "evt_level", "text": "pay", "priority": "high"}})
    client.flush(timeout=5)
    instruction = sent[0]["event"]["instruction"]
    assert instruction["id"] == "evt_level"
    assert set(instruction) == {"id", "hash"}
    client.close()
