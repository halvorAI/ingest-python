"""halvorai-ingest: signed Agent Payment Events for Python agents."""

from .client import ClientStats, HalvorClient
from .context import agent_context, current_context
from .init import InitResult, run_init
from .hash import evidence_sha256, hash_instruction
from .redact import redact_deep, redact_text
from .sign import generate_source_key, sign_event, verify_event

name = "halvorai-ingest"

__all__ = [
    "name",
    "HalvorClient",
    "ClientStats",
    "agent_context",
    "current_context",
    "hash_instruction",
    "evidence_sha256",
    "redact_text",
    "redact_deep",
    "generate_source_key",
    "sign_event",
    "verify_event",
    "run_init",
    "InitResult",
]
