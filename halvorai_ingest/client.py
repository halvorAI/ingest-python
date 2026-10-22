"""HalvorClient: queue, sign, batch, sink/HTTP delivery. emit() never raises."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

from .context import current_context
from .hash import hash_instruction
from .ids import new_event_id, now_utc
from .redact import redact_deep, redact_text
from .sign import sign_event
from .validate import validate_event

SDK_NAME = "halvorai-ingest@0.2.0"
DEFAULT_BATCH = 100
PRODUCTION_ENDPOINT = "https://api.halvorai.com"
SANDBOX_ENDPOINT = "https://api.sandbox.halvorai.com"


def default_endpoint(api_key: str | None, environment: str) -> str:
    """Live keys (or environment='live' without a key) go to production; test keys to the sandbox."""
    if api_key is not None:
        return PRODUCTION_ENDPOINT if api_key.startswith("hk_live_") else SANDBOX_ENDPOINT
    return PRODUCTION_ENDPOINT if environment == "live" else SANDBOX_ENDPOINT


def events_url(endpoint: str) -> str:
    """`endpoint` is a base URL, as in the TypeScript SDK; a full `/v1/events` URL is kept as is."""
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/events") else base + "/v1/events"


@dataclass
class ClientStats:
    emitted: int = 0
    queued: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    dropped_invalid: int = 0
    dropped_disabled: int = 0
    dropped_closed: int = 0


SinkFn = Callable[[list[dict[str, Any]]], None]


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

    _queue: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _stats: ClientStats = field(default_factory=ClientStats, init=False, repr=False)
    _source_jwk: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _source_id: str = field(default="src_unknown", init=False, repr=False)

    def __post_init__(self) -> None:
        # Like the TypeScript SDK, read keys from the environment (`init` writes them to .env.local).
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
        if self._source_jwk is not None:
            kid = self._source_jwk.get("kid")
            if isinstance(kid, str) and kid:
                self._source_id = kid

    @staticmethod
    def _parse_source_key(raw: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        if isinstance(raw, Mapping):
            return dict(raw)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
        return None

    def stats(self) -> ClientStats:
        return self._stats

    def notify(self, error: dict[str, Any]) -> None:
        if self.on_error is None:
            return
        try:
            self.on_error(error)
        except Exception:
            pass

    def emit(self, input_event: Any) -> str:
        event_id = new_event_id()
        try:
            if self._closed:
                self._stats.dropped_closed += 1
                self._stats.emitted += 1
                return event_id
            if self._source_jwk is None:
                self._stats.dropped_invalid += 1
                self._stats.emitted += 1
                self.notify({"code": "config", "message": "source_key missing or invalid"})
                return event_id
            if not isinstance(input_event, Mapping):
                self._stats.dropped_invalid += 1
                self._stats.emitted += 1
                self.notify({"code": "invalid_event", "message": "event must be a mapping"})
                return event_id
            if isinstance(input_event.get("id"), str):
                event_id = input_event["id"]
            cloned = deepcopy(dict(input_event))
            ctx = current_context()
            built = self._build_event(cloned, ctx, event_id)
            self._redact_event(built)
            problems = validate_event(built)
            if problems:
                self._stats.emitted += 1
                self._stats.dropped_invalid += 1
                self.notify(
                    {
                        "code": "invalid_event",
                        "message": "event failed schema validation",
                        "eventId": event_id,
                        "details": problems,
                    }
                )
                return event_id
            with self._lock:
                self._queue.append(built)
                self._stats.emitted += 1
                self._stats.queued = len(self._queue)
                should_flush = len(self._queue) >= self.batch_size
            if should_flush:
                self.flush()
            return event_id
        except Exception as exc:
            self._stats.emitted += 1
            self._stats.dropped_invalid += 1
            self.notify(
                {
                    "code": "invalid_event",
                    "message": str(exc) or "emit failed",
                    "eventId": event_id,
                }
            )
            return event_id

    def _build_event(
        self,
        input_event: dict[str, Any],
        ctx: Mapping[str, Any] | None,
        event_id: str,
    ) -> dict[str, Any]:
        instruction = self._build_instruction(input_event.get("instruction"), ctx)
        source_in = input_event.get("source")
        source: dict[str, Any] = {
            "id": self._source_id,
            "sdk": SDK_NAME,
            "adapter": "custom",
        }
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
            "agent": agent,
            "owner": owner,
            "payment": input_event.get("payment"),
            "payee": input_event.get("payee"),
        }
        if instruction is not None:
            event["instruction"] = instruction
        for key in ("instrument", "evidence", "related", "labels"):
            if key in input_event:
                event[key] = input_event[key]
        return event

    @staticmethod
    def _build_instruction(
        input_instruction: Any,
        ctx: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        raw = input_instruction if isinstance(input_instruction, Mapping) else None
        ctx_inst = ctx.get("instruction") if ctx else None
        if raw is None and not isinstance(ctx_inst, Mapping):
            return None
        base: dict[str, Any] = {}
        if isinstance(ctx_inst, Mapping):
            base.update(dict(ctx_inst))
        if raw is not None:
            base.update(dict(raw))
        text = base.get("text")
        if isinstance(text, str) and "hash" not in base:
            base["hash"] = hash_instruction(text)
        base.pop("text", None)
        if "id" not in base:
            return None
        return base

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
            ("instruction", "summary"),
        ):
            parent = event.get(path[0])
            if isinstance(parent, dict) and isinstance(parent.get(path[1]), str):
                parent[path[1]] = redact_text(parent[path[1]])
        if isinstance(event.get("labels"), dict):
            event["labels"] = redact_deep(event["labels"])

    def flush(self) -> dict[str, bool]:
        with self._lock:
            batch = self._queue[:]
            self._queue.clear()
            self._stats.queued = 0
        if not batch:
            return {"drained": True}
        assert self._source_jwk is not None
        envelopes: list[dict[str, Any]] = []
        for event in batch:
            try:
                sig = sign_event(event, self._source_jwk)
                envelopes.append({"event": event, "sig": sig})
            except Exception as exc:
                self._stats.dropped_invalid += 1
                self.notify(
                    {
                        "code": "invalid_event",
                        "message": str(exc) or "sign failed",
                        "eventId": event.get("id"),
                    }
                )
        if not envelopes:
            return {"drained": True}
        if self.sink is not None:
            try:
                self.sink(envelopes)
                self._stats.accepted += len(envelopes)
            except Exception as exc:
                self.notify({"code": "delivery_failed", "message": str(exc) or "sink failed"})
            return {"drained": True}
        return self._deliver_http(envelopes)

    def _deliver_http(self, envelopes: list[dict[str, Any]]) -> dict[str, bool]:
        if not self.api_key:
            self._stats.dropped_disabled += len(envelopes)
            self.notify({"code": "config", "message": "api_key required for HTTP delivery"})
            return {"drained": False}
        url = events_url(self.endpoint or default_endpoint(self.api_key, self.environment))
        try:
            with httpx.Client(timeout=30.0, transport=self.transport) as client:
                res = client.post(
                    url,
                    json={"events": envelopes},
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except Exception as exc:
            self.notify({"code": "delivery_failed", "message": str(exc) or "http failed"})
            return {"drained": False}
        if res.status_code >= 300:
            self._stats.rejected += len(envelopes)
            self.notify(
                {
                    "code": "unauthorized" if res.status_code in (401, 403) else "rejected",
                    "message": f"HTTP {res.status_code}",
                    "serverCode": _error_code(res) or str(res.status_code),
                }
            )
            return {"drained": False}
        self._apply_results(envelopes, res)
        return {"drained": True}

    def _apply_results(self, envelopes: list[dict[str, Any]], res: httpx.Response) -> None:
        """Count each event by the status the API gave it, as the TypeScript SDK does."""
        try:
            results = res.json().get("results", [])
        except Exception:
            results = []
        by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
        for envelope in envelopes:
            event_id = envelope["event"].get("id")
            result = by_id.get(event_id, {})
            status = result.get("status", "accepted")
            if status == "duplicate":
                self._stats.duplicates += 1
            elif status == "rejected":
                self._stats.rejected += 1
                error: dict[str, Any] = {
                    "code": "rejected",
                    "message": "server rejected event",
                    "eventId": event_id,
                }
                server = result.get("error")
                if isinstance(server, dict) and isinstance(server.get("code"), str):
                    error["serverCode"] = server["code"]
                self.notify(error)
            else:
                self._stats.accepted += 1

    def close(self) -> None:
        self._closed = True
        self.flush()


def _error_code(res: httpx.Response) -> str | None:
    try:
        code = res.json().get("error", {}).get("code")
    except Exception:
        return None
    return code if isinstance(code, str) else None
