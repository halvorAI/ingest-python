"""`halvorai-ingest demo` signs, verifies and rejects a changed event, without the instruction text."""

from __future__ import annotations

import pytest

from halvorai_ingest.cli import main
from halvorai_ingest.demo import run_demo


def test_demo_verifies_then_rejects_a_changed_event() -> None:
    lines: list[str] = []
    result = run_demo(log=lines.append)
    assert result.signed_verified and result.tampered_rejected
    text = "\n".join(lines)
    assert "Ed25519 signature: verified" in text
    assert "FAILED, the event was changed after signing" in text
    assert "Book a return flight" not in text


def test_demo_command_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    assert "1. Sign an event on this machine" in capsys.readouterr().out
