from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from halvorai_ingest import run_init, verify_event
from halvorai_ingest.cli import main
from halvorai_ingest.init import dashboard_for, upsert_env_file

API_KEY = "hk_test_" + "a" * 32


class FakeApi:
    def __init__(self, *, source_status: int = 201, events_status: int = 202) -> None:
        self.source_status = source_status
        self.events_status = events_status
        self.public_jwk: dict[str, Any] | None = None
        self.envelopes: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        body = json.loads(request.content)
        if request.url.path == "/v1/sources":
            assert body["adapter"] == "custom"
            self.public_jwk = body["public_jwk"]
            if self.source_status != 201:
                return httpx.Response(self.source_status, json={"error": {"code": "forbidden"}})
            return httpx.Response(201, json={"id": "src_01TEST"})
        if request.url.path == "/v1/events":
            self.envelopes.extend(body["events"])
            if self.events_status != 202:
                return httpx.Response(self.events_status, json={"error": {"code": "unauthorized"}})
            results = [{"id": e["event"]["id"], "status": "accepted"} for e in body["events"]]
            return httpx.Response(202, json={"results": results})
        return httpx.Response(404)


def test_init_registers_the_public_key_writes_env_and_sends_a_signed_event(tmp_path: Path) -> None:
    api = FakeApi()
    lines: list[str] = []
    result = run_init(API_KEY, cwd=tmp_path, name="ci box", transport=httpx.MockTransport(api), log=lines.append)

    assert api.urls[0] == "https://api.sandbox.halvorai.com/v1/sources"
    assert api.public_jwk is not None and "d" not in api.public_jwk
    assert result.source_id == "src_01TEST"
    assert result.environment == "test"
    assert result.dashboard_url == f"https://app.sandbox.halvorai.com/payments?event={result.event_id}"

    env = (tmp_path / ".env.local").read_text(encoding="utf-8")
    assert f"HALVOR_API_KEY={API_KEY}\n" in env
    assert "HALVOR_ENDPOINT" not in env
    raw = env.split("HALVOR_SOURCE_KEY=", 1)[1].strip()
    private_jwk = json.loads(raw.strip("'"))
    assert private_jwk["kid"] == "src_01TEST" and private_jwk["x"] == api.public_jwk["x"]

    [envelope] = api.envelopes
    assert envelope["event"]["id"] == result.event_id
    assert envelope["event"]["source"]["id"] == "src_01TEST"
    assert verify_event(envelope["event"], envelope["sig"], api.public_jwk)
    assert any("Source src_01TEST is ready." in line for line in lines)


def test_init_keeps_other_env_lines_and_saves_a_chosen_endpoint(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("OTHER=1\nHALVOR_API_KEY=old", encoding="utf-8")
    api = FakeApi()
    result = run_init(
        API_KEY,
        cwd=tmp_path,
        endpoint="http://127.0.0.1:8787/",
        transport=httpx.MockTransport(api),
        log=lambda _: None,
    )
    env = (tmp_path / ".env.local").read_text(encoding="utf-8")
    assert env.startswith("OTHER=1\n")
    assert env.count("HALVOR_API_KEY=") == 1 and "old" not in env
    assert "HALVOR_ENDPOINT=http://127.0.0.1:8787\n" in env
    assert api.urls[0] == "http://127.0.0.1:8787/v1/sources"
    assert result.dashboard_url.startswith("http://localhost:3000/payments?event=")


def test_init_reports_a_refused_registration_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"POST /v1/sources failed \(403\)"):
        run_init(API_KEY, cwd=tmp_path, transport=httpx.MockTransport(FakeApi(source_status=403)), log=lambda _: None)
    assert not (tmp_path / ".env.local").exists()


def test_init_fails_when_the_test_event_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="test event was not accepted"):
        run_init(API_KEY, cwd=tmp_path, transport=httpx.MockTransport(FakeApi(events_status=401)), log=lambda _: None)


def test_init_rejects_a_malformed_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hk_test_"):
        run_init("sk_live_nope", cwd=tmp_path, log=lambda _: None)


def test_cli_exits_non_zero_with_a_message(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["init", "--api-key", "bad", "--cwd", str(tmp_path)]) == 1
    assert "halvorai-ingest init failed" in capsys.readouterr().err


def test_dashboard_for_matches_the_node_sdk() -> None:
    assert dashboard_for("https://api.halvorai.com") == "https://app.halvorai.com"
    assert dashboard_for("https://api.sandbox.halvorai.com") == "https://app.sandbox.halvorai.com"
    assert dashboard_for("http://localhost:8787") == "http://localhost:3000"
    assert dashboard_for("https://api.example.com") == "https://app.example.com"
    assert dashboard_for("not a url") == "https://app.halvorai.com"


def test_upsert_quotes_values_with_spaces_or_quotes(tmp_path: Path) -> None:
    path = tmp_path / "nested" / ".env.local"
    upsert_env_file(path, {"A": "plain", "B": "it's spaced"})
    assert path.read_text(encoding="utf-8") == "A=plain\nB='it'\\''s spaced'\n"
