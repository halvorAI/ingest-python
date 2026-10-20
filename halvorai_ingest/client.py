"""HalvorClient: queue, sign, batch, sink/HTTP delivery. emit() never raises."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

import httpx

from .context import current_context
from .hash import hash_instruction
from .ids import new_event_id, now_utc
from .redact import redact_deep, redact_text
from .sign import sign_event

SDK_NAME = "halvorai-ingest@0.1.0"
DEFAULT_BATCH = 100
DEFAULT_ENDPOINT = "https://api.halvorai.com/v1/events"


@dataclass
class ClientStats:
    emitted: int = 0
    queued: int = 0
    accepted: int = 0
    rejected: int = 0
    dropped_invalid: int = 0
    dropped_closed: int = 0


SinkFn = Callable[[list[dict[str, Any]]], None]


@dataclass
class HalvorClient:
    api_key: str | None = None
    source_key: str | Mapping[str, Any] | None = None
    environment: str = "test"
    endpoint: str = DEFAULT_ENDPOINT
    sink: SinkFn | None = None
    batch_size: int = DEFAULT_BATCH
    on_error: Callable[[dict[str, Any]], None] | None = None

    _queue: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _stats: ClientStats = field(default_factory=ClientStats, init=False, repr=False)
    _source_jwk: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _source_id: str = field(default="src_unknown", init=False, repr=False)

    def __post_init__(self) -> None:
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
            self.notify({"code": "config", "message": "api_key required for HTTP delivery"})
            return {"drained": False}
        url = self.endpoint
        if not url.endswith("/events") and not url.rstrip("/").endswith("events"):
            url = urljoin(url.rstrip("/") + "/", "events")
        try:
            with httpx.Client(timeout=30.0) as client:
                res = client.post(
                    url,
                    json={"events": envelopes},
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
            if res.status_code < 300:
                self._stats.accepted += len(envelopes)
                return {"drained": True}
            self._stats.rejected += len(envelopes)
            self.notify(
                {
                    "code": "rejected",
                    "message": f"HTTP {res.status_code}",
                    "serverCode": str(res.status_code),
                }
            )
            return {"drained": False}
        except Exception as exc:
            self.notify({"code": "delivery_failed", "message": str(exc) or "http failed"})
            return {"drained": False}

    def close(self) -> None:
        self._closed = True
        self.flush()
