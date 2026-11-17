"""`halvorai-ingest init`: register this machine as a source, as `npx @halvorai/ingest init` does."""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx

from .client import API_KEY_PATTERN, HalvorClient, default_endpoint
from .sign import generate_source_key

DASHBOARD_URL = "https://app.halvorai.com"
SANDBOX_DASHBOARD_URL = "https://app.sandbox.halvorai.com"


@dataclass
class InitResult:
    source_id: str
    event_id: str
    env_path: Path
    dashboard_url: str
    endpoint: str
    environment: str


def dashboard_for(endpoint: str) -> str:
    """The dashboard that belongs to an API endpoint, as in the Node.js SDK."""
    url = urlsplit(endpoint)
    host = url.hostname or ""
    if host == "api.halvorai.com" or not url.scheme:
        return DASHBOARD_URL
    if host == "api.sandbox.halvorai.com":
        return SANDBOX_DASHBOARD_URL
    # The local dashboard is signed in to at localhost:3000, whatever loopback name the API has.
    if url.port == 8787:
        loopback = host in ("127.0.0.1", "localhost", "::1")
        return f"{url.scheme}://{'localhost' if loopback else host}:3000"
    if host.startswith("api."):
        return f"{url.scheme}://app.{host[4:]}"
    return f"{url.scheme}://{url.netloc}"


def upsert_env_file(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in a dotenv file, keeping every other line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if content and not content.endswith("\n"):
        content += "\n"
    for key, value in values.items():
        quoted = (
            "'" + value.replace("'", "'\\''") + "'"
            if re.search(r"[\s\"'`#]", value)
            else value
        )
        line = f"{key}={quoted}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(lambda _: line, content, count=1)
        else:
            content += line + "\n"
    path.write_text(content, encoding="utf-8")


def _register_source(
    http: httpx.Client, endpoint: str, api_key: str, name: str, public_jwk: dict[str, str]
) -> str:
    res = http.post(
        f"{endpoint}/v1/sources",
        json={"name": name, "adapter": "custom", "public_jwk": public_jwk},
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    if res.status_code != 201:
        raise RuntimeError(f"POST /v1/sources failed ({res.status_code}): {res.text or res.reason_phrase}")
    source_id = res.json().get("id")
    if not isinstance(source_id, str) or not source_id.startswith("src_"):
        raise RuntimeError("POST /v1/sources returned no source id")
    return source_id


def run_init(
    api_key: str,
    *,
    cwd: str | Path = ".",
    endpoint: str | None = None,
    name: str | None = None,
    dashboard: str | None = None,
    transport: httpx.BaseTransport | None = None,
    log: Callable[[str], None] = print,
) -> InitResult:
    """
    Generate an Ed25519 source key, register its public half (`POST /v1/sources`), write
    `HALVOR_API_KEY` and `HALVOR_SOURCE_KEY` to `.env.local`, and send one test event.
    The private key never leaves this machine.
    """
    if not API_KEY_PATTERN.match(api_key):
        raise ValueError("api key must look like hk_test_… or hk_live_…")
    environment = "live" if api_key.startswith("hk_live_") else "test"
    base = (endpoint or default_endpoint(api_key, environment)).rstrip("/")
    source_name = ((name or "").strip() or socket.gethostname() or "local")[:100]

    log("Generating Ed25519 source key…")
    private_jwk: dict[str, Any] = dict(generate_source_key())
    public_jwk = {k: v for k, v in private_jwk.items() if k not in ("d", "kid")}

    log(f"Registering source at {base}…")
    with httpx.Client(timeout=30.0, transport=transport) as http:
        source_id = _register_source(http, base, api_key, source_name, public_jwk)
    private_jwk["kid"] = source_id

    env_path = Path(cwd).resolve() / ".env.local"
    values = {"HALVOR_API_KEY": api_key, "HALVOR_SOURCE_KEY": json.dumps(private_jwk, separators=(",", ":"))}
    # Saved only when chosen, so a later HalvorClient() talks to the same API.
    if endpoint is not None:
        values["HALVOR_ENDPOINT"] = base
    upsert_env_file(env_path, values)
    log(f"Wrote {env_path}")

    log("Sending a test event…")
    errors: list[str] = []
    client = HalvorClient(
        api_key=api_key,
        source_key=private_jwk,
        endpoint=base,
        transport=transport,
        exit_hook=False,
        on_error=lambda e: errors.append(f"{e.get('code')}: {e.get('message')}"),
    )
    event_id = client.emit(
        {
            "type": "payment.settled",
            "agent": {"id": "agt_halvor_init", "name": "HalvorAI init"},
            "owner": {"id": "own_halvor_init", "type": "business", "display_name": "HalvorAI init"},
            "payment": {
                "payment_id": f"init:{source_id}",
                "rail": "x402",
                "amount": {
                    "value": "1",
                    "unit": "atomic",
                    "asset": {
                        "network": "eip155:84532",
                        "address": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                        "symbol": "USDC",
                        "decimals": 6,
                    },
                },
                "status": "settled",
                "description": "HalvorAI init test event",
                "rail_ref": {"scheme": "exact", "network": "eip155:84532"},
            },
            "payee": {"type": "api_provider", "name": "HalvorAI init"},
        }
    )
    flushed = client.flush(timeout=30)
    client.close(timeout=1)
    if not flushed["drained"] or errors:
        raise RuntimeError(f"test event was not accepted: {'; '.join(errors) or 'flush timed out'}")

    dashboard_url = f"{(dashboard or dashboard_for(base)).rstrip('/')}/payments?event={quote(event_id)}"
    log("")
    log(f"Source {source_id} is ready.")
    log(f"See it in the dashboard: {dashboard_url}")
    log(f"First event id: {event_id}")
    return InitResult(source_id, event_id, env_path, dashboard_url, base, environment)
