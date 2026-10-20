"""Agent context via contextvars (AsyncLocalStorage equivalent)."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator, Mapping, TypedDict

_store: ContextVar[dict[str, Any] | None] = ContextVar("halvor_agent_context", default=None)


class AgentRef(TypedDict, total=False):
    id: str
    name: str
    framework: str
    version: str
    key_thumbprint: str


class OwnerRef(TypedDict, total=False):
    id: str
    type: str
    display_name: str
    country: str
    kyc_ref: str


class InstructionRef(TypedDict, total=False):
    id: str
    text: str
    summary: str
    channel: str
    issued_at: str
    tool_call_id: str


class AgentContext(TypedDict, total=False):
    agent: AgentRef
    owner: OwnerRef
    instruction: InstructionRef


def current_context() -> AgentContext | None:
    ctx = _store.get()
    return None if ctx is None else ctx  # type: ignore[return-value]


def _merge(outer: Mapping[str, Any] | None, ctx: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    agent = ctx.get("agent") or (outer.get("agent") if outer else None)
    owner = ctx.get("owner") or (outer.get("owner") if outer else None)
    instruction = ctx.get("instruction") or (outer.get("instruction") if outer else None)
    if agent is not None:
        merged["agent"] = agent
    if owner is not None:
        merged["owner"] = owner
    if instruction is not None:
        merged["instruction"] = instruction
    return merged


@contextmanager
def agent_context(**ctx: Any) -> Iterator[None]:
    """Run a block with an agent context. Nested calls merge over the outer context."""
    outer = _store.get()
    token: Token[dict[str, Any] | None] = _store.set(_merge(outer, ctx))
    try:
        yield
    finally:
        _store.reset(token)
