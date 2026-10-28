"""HalvorClient: validate, queue, sign and deliver Agent Payment Events.

`emit()` never raises and never does network or disk I/O on the caller's thread. A background
worker signs and delivers batches, with the same retry, split, spool and dead-letter rules as the
Node.js SDK (`@halvorai/ingest`).
"""

from __future__ import annotations

import atexit
import dataclasses
import json
import os
import random
import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

import httpx

from .context import current_context
from .hash import evidence_sha256, hash_instruction
from .ids import new_event_id, now_utc
from .redact import redact_deep, redact_text
from .sign import sign_event
from .spool import DEFAULT_MAX_AGE_S, DEFAULT_MAX_BYTES, Spool
from .validate import validate_event


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("halvorai-ingest")
    except Exception:
        return "0.2.0"


SDK_NAME = f"halvorai-ingest@{_package_version()}"
DEFAULT_BATCH = 100
DEFAULT_FLUSH_INTERVAL = 1.0
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
EXIT_FLUSH_TIMEOUT = 2.0
PRODUCTION_ENDPOINT = "https://api.halvorai.com"
SANDBOX_ENDPOINT = "https://api.sandbox.halvorai.com"

API_KEY_PATTERN = re.compile(r"^hk_(test|live)_[0-9A-Za-z]{32}$")
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Statuses that fail the same way however often the batch is sent.
FATAL_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 410, 415, 422})
INSTRUCTION_FIELDS = ("id", "hash", "summary", "channel", "issued_at", "tool_call_id")


def default_endpoint(api_key: str | None, environment: str) -> str:
    """Live keys (or environment='live' without a key) go to production; test keys to the sandbox."""
    if api_key is not None:
        return PRODUCTION_ENDPOINT if api_key.startswith("hk_live_") else SANDBOX_ENDPOINT
    return PRODUCTION_ENDPOINT if environment == "live" else SANDBOX_ENDPOINT


def events_url(endpoint: str) -> str:
    """`endpoint` is a base URL, as in the Node.js SDK; a full `/v1/events` URL is kept as is."""
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/events") else base + "/v1/events"


# JSON evidence that carries a signature over its own fields, so redaction would break it.
SIGNED_JSON_EVIDENCE = frozenset({"x402.payment_payload"})


@dataclass
class ClientStats:
    emitted: int = 0
    queued: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    #: Events the API will never accept, or that could not be delivered or spooled.
    dropped: int = 0
    dropped_invalid: int = 0
    dropped_disabled: int = 0
    dropped_closed: int = 0
    retries: int = 0
    spooled: int = 0
    replayed: int = 0


SinkFn = Callable[[list[dict[str, Any]]], None]
Envelope = dict[str, Any]


@dataclass
class _Outcome:
    kind: Literal["ok", "retry", "fatal", "too_large"]
    status: int | None = None
    results: list[Any] = field(default_factory=list)
    error: dict[str, Any] = field(default_factory=dict)


@dataclass
class HalvorClient:
    api_key: str | None = None
    source_key: str | Mapping[str, Any] | None = None
    environment: str = "test"
    #: Base URL of the API. Defaults to HALVOR_ENDPOINT, else sandbox for test keys, production for live.
    endpoint: str | None = None
    sink: SinkFn | None = None
    batch_size: int = DEFAULT_BATCH
    on_error: Callable[[dict[str, Any]], None] | None = None
    #: For tests: an httpx transport (e.g. httpx.MockTransport).
    transport: httpx.BaseTransport | None = None
    #: Seconds between background sends.
    flush_interval: float = DEFAULT_FLUSH_INTERVAL
    #: Backoff before each retry, in seconds; up to +20 % jitter is added.
    retry_delays: tuple[float, ...] | list[float] = DEFAULT_RETRY_DELAYS
    #: Folder for events that could not be delivered during an outage; sent again later.
    spool_dir: str | None = None
    spool_max_bytes: int = DEFAULT_MAX_BYTES
    spool_max_age: float = DEFAULT_MAX_AGE_S
    #: Flush (up to 2 s) when the interpreter exits normally.
    exit_hook: bool = True

    _queue: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cond: threading.Condition = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _disabled: bool = field(default=False, init=False, repr=False)
    _config_reported: bool = field(default=False, init=False, repr=False)
    _stats: ClientStats = field(default_factory=ClientStats, init=False, repr=False)
    _source_jwk: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _source_id: str = field(default="src_unknown", init=False, repr=False)
    _worker: threading.Thread | None = field(default=None, init=False, repr=False)
    _stopping: bool = field(default=False, init=False, repr=False)
    _inflight: int = field(default=0, init=False, repr=False)
    _send_now: bool = field(default=False, init=False, repr=False)
    _replay_requested: bool = field(default=False, init=False, repr=False)
    _spool_cooldown_until: float = field(default=0.0, init=False, repr=False)
    _http: httpx.Client | None = field(default=None, init=False, repr=False)
    _spool: Spool | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._cond = threading.Condition(self._lock)
        # Like the Node.js SDK, read keys from the environment (`init` writes them to .env.local).
        if self.api_key is None:
            self.api_key = os.environ.get("HALVOR_API_KEY") or None
        if self.source_key is None:
            self.source_key = os.environ.get("HALVOR_SOURCE_KEY") or None
        if self.endpoint is None:
            self.endpoint = os.environ.get("HALVOR_ENDPOINT") or default_endpoint(
                self.api_key, self.environment
            )
        if self.api_key is not None and self.environment == "test" and self.api_key.startswith("hk_live_"):
            self.environment = "live"

        self._source_jwk = self._parse_source_key(self.source_key)
        if self._source_jwk is None:
            self._disable(
                "source_key is missing or is not a private Ed25519 JWK (kty OKP, crv Ed25519) with a kid"
            )
        else:
            self._source_id = str(self._source_jwk["kid"])
        if self.sink is None and not self._disabled:
            if self.api_key is None or not API_KEY_PATTERN.match(self.api_key):
                self._disable("api_key is missing or malformed (expected hk_test_… or hk_live_…)")

        if self.spool_dir is not None:
            self._spool = Spool(
                self.spool_dir, max_bytes=self.spool_max_bytes, max_age_s=self.spool_max_age
            )
        if self.exit_hook:
            atexit.register(self._exit_flush)
        if not self._disabled and self._spool is not None and self._spool.files():
            # Replay what a previous process left behind.
            with self._cond:
                self._replay_requested = True
                self._ensure_worker()

    # ------------------------------------------------------------------ config

    @staticmethod
    def _parse_source_key(raw: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        jwk: Any
        if isinstance(raw, Mapping):
            jwk = dict(raw)
        else:
            try:
                jwk = json.loads(raw)
            except ValueError:
                return None
        if not isinstance(jwk, dict):
            return None
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            return None
        for key in ("d", "kid"):
            if not isinstance(jwk.get(key), str) or not jwk[key]:
                return None
        return jwk

    def _disable(self, message: str) -> None:
        self._disabled = True
        if not self._config_reported:
            self._config_reported = True
            self.notify({"code": "config", "message": message})

    # ------------------------------------------------------------------ public API

    def stats(self) -> ClientStats:
        """A snapshot of the counters."""
        with self._lock:
            snapshot = dataclasses.replace(self._stats)
            snapshot.queued = len(self._queue) + self._inflight
        return snapshot

    def notify(self, error: dict[str, Any]) -> None:
        if self.on_error is None:
            return
        try:
            self.on_error(error)
        except Exception:
            pass

    def emit(self, input_event: Any) -> str:
        """Queue one event and return its id at once. Never raises, never blocks on I/O."""
        event_id = new_event_id()
        try:
            if self._closed:
                self._count("emitted", "dropped_closed")
                return event_id
            if self._disabled:
                self._count("emitted", "dropped_disabled")
                return event_id
            if not isinstance(input_event, Mapping):
                self._count("emitted", "dropped_invalid")
                self.notify({"code": "invalid_event", "message": "event must be a mapping"})
                return event_id
            if isinstance(input_event.get("id"), str):
                event_id = input_event["id"]
            cloned = deepcopy(dict(input_event))
            built = self._build_event(cloned, current_context(), event_id)
            self._redact_event(built)
            problems = validate_event(built)
            if problems:
                self._count("emitted", "dropped_invalid")
                self.notify(
                    {
                        "code": "invalid_event",
                        "message": "event failed schema validation",
                        "eventId": event_id,
                        "details": problems,
                    }
                )
                return event_id
            with self._cond:
                self._queue.append(built)
                self._stats.emitted += 1
                self._ensure_worker()
                if len(self._queue) >= self.batch_size:
                    self._send_now = True
                    self._cond.notify_all()
            return event_id
        except Exception as exc:
            self._count("emitted", "dropped_invalid")
            self.notify({"code": "invalid_event", "message": str(exc) or "emit failed", "eventId": event_id})
            return event_id

    def flush(self, timeout: float | None = None, *, replay_spool: bool = True) -> dict[str, bool]:
        """
        Send everything queued and wait for it. Without `timeout` this waits for the full retry
        schedule (about a minute per batch while the API is down); in request handlers pass one,
        for example `flush(timeout=5)`. Undelivered events stay queued or spooled.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if self._worker is None and not self._queue and not (replay_spool and self._spool):
                return {"drained": True}
            self._send_now = True
            if replay_spool and self._spool is not None:
                self._replay_requested = True
                self._spool_cooldown_until = 0.0
            self._ensure_worker()
            self._cond.notify_all()
            while self._queue or self._inflight or self._send_now or self._replay_requested:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return {"drained": False}
                self._cond.wait(remaining)
            return {"drained": True}

    def close(self, timeout: float | None = None) -> None:
        """Refuse new events, send what is queued (the spool stays on disk), and stop the worker."""
        self._closed = True
        try:
            self.flush(timeout, replay_spool=False)
        finally:
            with self._cond:
                self._stopping = True
                self._cond.notify_all()
            if self.exit_hook:
                atexit.unregister(self._exit_flush)
            if self._http is not None:
                self._http.close()
                self._http = None

    def __enter__(self) -> HalvorClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _exit_flush(self) -> None:
        try:
            self.flush(EXIT_FLUSH_TIMEOUT, replay_spool=False)
        except Exception:
            pass

    # ------------------------------------------------------------------ building

    def _count(self, *names: str) -> None:
        with self._lock:
            for name in names:
                setattr(self._stats, name, getattr(self._stats, name) + 1)

    def _build_event(
        self,
        input_event: dict[str, Any],
        ctx: Mapping[str, Any] | None,
        event_id: str,
    ) -> dict[str, Any]:
        instruction = self._build_instruction(input_event.get("instruction"), ctx)
        source: dict[str, Any] = {"id": self._source_id, "sdk": SDK_NAME, "adapter": "custom"}
        source_in = input_event.get("source")
        if isinstance(source_in, Mapping):
            source.update(dict(source_in))
            source["id"] = self._source_id
            source["sdk"] = SDK_NAME

        agent = input_event.get("agent") or (ctx.get("agent") if ctx else None) or {"id": "unattributed"}
        owner = (
            input_event.get("owner")
            or (ctx.get("owner") if ctx else None)
            or {"id": "unattributed", "type": "business"}
        )
        event: dict[str, Any] = {
            "ape_version": "0.1",
            "id": event_id,
            "type": input_event.get("type"),
            "occurred_at": input_event.get("occurred_at")
            if isinstance(input_event.get("occurred_at"), str)
            else now_utc(),
            "environment": input_event.get("environment") or self.environment,
            "source": source,
            "agent": deepcopy(agent),
            "owner": deepcopy(owner),
            "payment": input_event.get("payment"),
            "payee": input_event.get("payee"),
        }
        if instruction is not None:
            event["instruction"] = instruction
        for key in ("instrument", "evidence", "related", "labels"):
            if input_event.get(key) is not None:
                event[key] = input_event[key]
        return event

    @staticmethod
    def _build_instruction(
        input_instruction: Any,
        ctx: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """An event-level instruction replaces the context one, as in the Node.js SDK.

        Only the APE instruction fields are kept; `text` is hashed locally and never sent.
        """
        raw = input_instruction if isinstance(input_instruction, Mapping) else None
        if raw is None and ctx is not None and isinstance(ctx.get("instruction"), Mapping):
            raw = ctx["instruction"]
        if raw is None or not isinstance(raw.get("id"), str):
            return None
        out: dict[str, Any] = {}
        hash_value = raw.get("hash")
        if not isinstance(hash_value, str) and isinstance(raw.get("text"), str):
            hash_value = hash_instruction(raw["text"])
        if not isinstance(hash_value, str):
            return None
        for key in INSTRUCTION_FIELDS:
            value = hash_value if key == "hash" else raw.get(key)
            if isinstance(value, str):
                out[key] = value
        return out

    def _redact_event(self, event: dict[str, Any]) -> None:
        payment = event.get("payment")
        if isinstance(payment, dict):
            for key in ("description", "category", "failure_reason"):
                if isinstance(payment.get(key), str):
                    payment[key] = redact_text(payment[key])
        for path in (
            ("agent", "name"),
            ("owner", "display_name"),
            ("payee", "name"),
            ("payee", "website"),
            ("payee", "resource_url"),
            ("instruction", "summary"),
        ):
            parent = event.get(path[0])
            if isinstance(parent, dict) and isinstance(parent.get(path[1]), str):
                parent[path[1]] = redact_text(parent[path[1]])
        labels = event.get("labels")
        if isinstance(labels, dict):
            # Keys are free text too (docs/FIELDS.md).
            event["labels"] = {
                redact_text(str(k)): redact_deep(v) for k, v in labels.items()
            }
        evidence = event.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                # A signed x402 payload is evidence only while byte-identical.
                if isinstance(item, dict) and item.get("kind") in SIGNED_JSON_EVIDENCE:
                    continue
                if isinstance(item, dict) and item.get("format") == "json" and "value" in item:
                    item["value"] = redact_deep(item["value"])
                    item["sha256"] = evidence_sha256(item["value"])

    # ------------------------------------------------------------------ worker

    def _ensure_worker(self) -> None:
        """Start the background worker. Call with the lock held."""
        if self._worker is None or not self._worker.is_alive():
            self._stopping = False
            self._worker = threading.Thread(target=self._run, name="halvorai-ingest", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                idle = not self._send_now and not self._replay_requested
                if idle and len(self._queue) < self.batch_size:
                    if self._stopping and not self._queue:
                        return
                    # Batch up for one interval; a full batch or flush() wakes the worker early.
                    self._cond.wait(self.flush_interval)
                replay = self._replay_requested and time.time() >= self._spool_cooldown_until
                batch = self._queue[: self.batch_size]
                del self._queue[: self.batch_size]
                self._inflight += len(batch)
            try:
                if replay:
                    self._replay_spool()
                if batch:
                    self._deliver_queued(batch)
            except Exception as exc:  # the worker must never die
                self.notify({"code": "delivery_failed", "message": str(exc) or "worker error"})
            finally:
                with self._cond:
                    self._inflight -= len(batch)
                    if replay:
                        self._replay_requested = False
                    if not self._queue:
                        self._send_now = False
                        if not replay and self._replay_requested and time.time() < self._spool_cooldown_until:
                            # Spool is cooling down after an outage; don't hold flush() for it.
                            self._replay_requested = False
                    self._cond.notify_all()

    def _sign(self, events: list[dict[str, Any]]) -> list[Envelope]:
        assert self._source_jwk is not None
        envelopes: list[Envelope] = []
        for event in events:
            try:
                envelopes.append({"event": event, "sig": sign_event(event, self._source_jwk)})
            except Exception as exc:
                self._count("dropped_invalid")
                self.notify({"code": "invalid_event", "message": str(exc) or "sign failed", "eventId": event.get("id")})
        return envelopes

    def _deliver_queued(self, events: list[dict[str, Any]]) -> None:
        envelopes = self._sign(events)
        pending, reason = self._deliver(envelopes)
        self._park(pending, reason)

    # ------------------------------------------------------------------ delivery

    def _deliver(self, envelopes: list[Envelope]) -> tuple[list[Envelope], str | None]:
        """Returns the envelopes to try again later. Everything else is settled."""
        if not envelopes:
            return [], None
        if self.sink is not None:
            return self._deliver_sink(envelopes)
        return self._deliver_http(envelopes)

    def _sleep_before_retry(self, attempt: int) -> None:
        with self._lock:
            self._stats.retries += 1
        base = self.retry_delays[attempt]
        time.sleep(base * (1 + random.random() * 0.2))

    def _deliver_sink(self, envelopes: list[Envelope]) -> tuple[list[Envelope], str | None]:
        assert self.sink is not None
        for attempt in range(len(self.retry_delays) + 1):
            try:
                self.sink(envelopes)
                with self._lock:
                    self._stats.accepted += len(envelopes)
                return [], None
            except Exception as exc:
                if attempt >= len(self.retry_delays):
                    return envelopes, str(exc) or "sink failed"
                self._sleep_before_retry(attempt)
        return envelopes, "sink failed"

    def _deliver_http(self, envelopes: list[Envelope]) -> tuple[list[Envelope], str | None]:
        for attempt in range(len(self.retry_delays) + 1):
            outcome = self._post(envelopes)
            if outcome.kind == "ok":
                self._apply_results(envelopes, outcome.results)
                return [], None
            if outcome.kind == "too_large":
                if len(envelopes) == 1:
                    error = dict(outcome.error)
                    error["code"] = error.get("code") or "payload_too_large"
                    error["message"] = "event is larger than the API accepts"
                    self._drop_fatal(envelopes, "delivery_failed", error)
                    return [], None
                mid = (len(envelopes) + 1) // 2
                left, left_reason = self._deliver_http(envelopes[:mid])
                right, right_reason = self._deliver_http(envelopes[mid:])
                return left + right, right_reason or left_reason
            if outcome.kind == "fatal":
                code = "unauthorized" if outcome.status in (401, 403) else "delivery_failed"
                self._drop_fatal(envelopes, code, outcome.error)
                return [], None
            if attempt >= len(self.retry_delays):
                return envelopes, outcome.error.get("message")
            self._sleep_before_retry(attempt)
        return envelopes, "delivery failed"

    def _post(self, envelopes: list[Envelope]) -> _Outcome:
        url = events_url(self.endpoint or default_endpoint(self.api_key, self.environment))
        if self._http is None:
            self._http = httpx.Client(timeout=30.0, transport=self.transport)
        try:
            res = self._http.post(
                url,
                content=json.dumps({"events": envelopes}, separators=(",", ":"), ensure_ascii=False).encode(
                    "utf-8"
                ),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except Exception as exc:
            return _Outcome("retry", error={"message": str(exc) or "network error"})
        status = res.status_code
        if status == 413:
            return _Outcome("too_large", status, error=_read_error(res))
        if status in FATAL_STATUSES:
            return _Outcome("fatal", status, error=_read_error(res))
        if status in RETRY_STATUSES or not res.is_success:
            return _Outcome("retry", status, error=_read_error(res))
        try:
            body = res.json()
            results = body.get("results", []) if isinstance(body, dict) else []
        except ValueError:
            return _Outcome("retry", status, error={"message": "invalid response body"})
        return _Outcome("ok", status, results=results if isinstance(results, list) else [])

    def _apply_results(self, envelopes: list[Envelope], results: list[Any]) -> None:
        """Count each event by the status the API gave it, as the Node.js SDK does."""
        by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
        for envelope in envelopes:
            event_id = envelope["event"].get("id")
            result = by_id.get(event_id, {})
            status = result.get("status", "accepted")
            with self._lock:
                if status == "duplicate":
                    self._stats.duplicates += 1
                elif status == "rejected":
                    self._stats.rejected += 1
                else:
                    self._stats.accepted += 1
            if status == "rejected":
                error: dict[str, Any] = {"code": "rejected", "message": "server rejected event", "eventId": event_id}
                server = result.get("error")
                if isinstance(server, dict) and isinstance(server.get("code"), str):
                    error["serverCode"] = server["code"]
                self.notify(error)

    def _drop_fatal(self, envelopes: list[Envelope], code: str, error: Mapping[str, Any]) -> None:
        """Counts a batch the API will never accept as dropped, once, and keeps a copy in `dead/`."""
        with self._lock:
            self._stats.dropped += len(envelopes)
        report: dict[str, Any] = {"code": code, "message": error.get("message") or "delivery failed"}
        if error.get("code"):
            report["serverCode"] = error["code"]
        if error.get("details") is not None:
            report["details"] = error["details"]
        if error.get("requestId"):
            report["requestId"] = error["requestId"]
        if len(envelopes) == 1:
            report["eventId"] = envelopes[0]["event"].get("id")
        self.notify(report)
        if self._spool is not None:
            try:
                self._spool.dead_letter(envelopes)
            except OSError as exc:
                self.notify({"code": "spool", "message": str(exc) or "dead-letter write failed"})

    def _park(self, pending: list[Envelope], reason: str | None) -> None:
        """Spools envelopes that could not be delivered now, or drops them loudly without a spool."""
        if not pending:
            return
        if self._spool is not None:
            try:
                evicted = self._spool.write(pending)
                with self._lock:
                    self._stats.spooled += len(pending)
                    self._stats.dropped += evicted.events
                if evicted.events:
                    self.notify(
                        {
                            "code": "spool",
                            "message": f"spool limit reached: evicted {evicted.events} oldest event(s) "
                            f"in {evicted.files} file(s)",
                        }
                    )
                return
            except OSError as exc:
                self.notify({"code": "spool", "message": str(exc) or "spool write failed"})
        with self._lock:
            self._stats.dropped += len(pending)
        self.notify({"code": "delivery_failed", "message": reason or "delivery failed"})

    def _replay_spool(self) -> None:
        """Sends spooled files oldest first, one at a time. See the Node.js SDK for the same rules."""
        spool = self._spool
        if spool is None or self._disabled:
            return
        try:
            names = spool.files()
        except OSError as exc:
            self.notify({"code": "spool", "message": str(exc) or "spool read failed"})
            return
        for name in names:
            file = spool.read(name)
            if file.corrupt:
                self.notify({"code": "spool", "message": f"skipped {file.corrupt} corrupt spool line(s)"})
            if not file.envelopes:
                spool.remove(name)
                continue
            with self._lock:
                dropped_before = self._stats.dropped
            pending, _ = self._deliver(file.envelopes)
            with self._lock:
                dropped = self._stats.dropped - dropped_before
                self._stats.replayed += len(file.envelopes) - len(pending) - dropped
            if not pending:
                spool.remove(name)
                continue
            # API still unavailable: keep only what was not delivered, then back off.
            spool.rewrite(name, pending)
            delays = list(self.retry_delays)
            self._spool_cooldown_until = time.time() + (delays[-1] if delays else 32.0)
            return


def _read_error(res: httpx.Response) -> dict[str, Any]:
    """The API's error body, `{ error: { code, message, details, request_id } }`, when it sent one."""
    text = ""
    try:
        text = res.text
    except Exception:
        pass
    fallback = text.strip()[:500] or f"HTTP {res.status_code}"
    out: dict[str, Any] = {"message": fallback}
    header_id = res.headers.get("request-id")
    if header_id:
        out["requestId"] = header_id
    try:
        body = json.loads(text)
    except ValueError:
        return out
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        if isinstance(error.get("message"), str) and error["message"]:
            out["message"] = error["message"]
        if isinstance(error.get("code"), str):
            out["code"] = error["code"]
        if isinstance(error.get("details"), list):
            out["details"] = error["details"]
        if isinstance(error.get("request_id"), str):
            out["requestId"] = error["request_id"]
    return out
