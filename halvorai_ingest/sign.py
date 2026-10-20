"""Detached APE JWS (Ed25519 / EdDSA) over JCS, matching `@halvorai/schema` sign.ts."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import canonicalize

HEADER_TYP = "ape+jws"
APE_VERSION = "0.1"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _source_id(event: Mapping[str, Any]) -> str:
    source = event.get("source")
    if not isinstance(source, Mapping):
        raise TypeError("event.source.id is required to sign an event")
    sid = source.get("id")
    if not isinstance(sid, str) or len(sid) == 0:
        raise TypeError("event.source.id is required to sign an event")
    return sid


def private_key_from_jwk(jwk: Mapping[str, Any]) -> Ed25519PrivateKey:
    d = jwk.get("d")
    if not isinstance(d, str):
        raise TypeError("private JWK requires d")
    return Ed25519PrivateKey.from_private_bytes(_b64url_decode(d))


def public_key_from_jwk(jwk: Mapping[str, Any]) -> Ed25519PublicKey:
    x = jwk.get("x")
    if not isinstance(x, str):
        raise TypeError("public JWK requires x")
    return Ed25519PublicKey.from_public_bytes(_b64url_decode(x))


def sign_event(event: Mapping[str, Any], private_jwk: Mapping[str, Any]) -> str:
    """Sign the JCS form of an event; return detached compact JWS (`header..sig`)."""
    header = {
        "alg": "EdDSA",
        "kid": _source_id(event),
        "typ": HEADER_TYP,
        "ape": APE_VERSION,
    }
    # Compact JSON without spaces; key order matches sign.ts setProtectedHeader.
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(canonicalize(event).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = private_key_from_jwk(private_jwk).sign(signing_input)
    return f"{header_b64}..{_b64url_encode(sig)}"


def verify_event(event: Mapping[str, Any], sig: str, public_jwk: Mapping[str, Any]) -> bool:
    """Return True only for a valid detached APE JWS over this exact event. Never raises."""
    try:
        parts = sig.split(".")
        if len(parts) != 3:
            return False
        header_b64, payload, signature_b64 = parts
        if payload != "" or not header_b64 or not signature_b64:
            return False
        header = json.loads(_b64url_decode(header_b64))
        if (
            header.get("alg") != "EdDSA"
            or header.get("typ") != HEADER_TYP
            or header.get("ape") != APE_VERSION
            or header.get("kid") != _source_id(event)
        ):
            return False
        payload_b64 = _b64url_encode(canonicalize(event).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        public_key_from_jwk(public_jwk).verify(_b64url_decode(signature_b64), signing_input)
        return True
    except Exception:
        return False


def generate_source_key(kid: str = "src_local") -> dict[str, str]:
    """Create a new Ed25519 private JWK for signing events. Drop `d` to get the public half."""
    key = Ed25519PrivateKey.generate()
    raw_private = key.private_bytes_raw()
    raw_public = key.public_key().public_bytes_raw()
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _b64url_encode(raw_public),
        "d": _b64url_encode(raw_private),
        "kid": kid,
    }
