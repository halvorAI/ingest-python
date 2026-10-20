from halvorai_ingest.redact import redact_text


def test_redacts_pan() -> None:
    # Visa test PAN (Luhn-valid)
    assert "[redacted:pan]" in redact_text("card 4111111111111111 please")


def test_redacts_secret_key() -> None:
    out = redact_text("key=sk_test_abcdefghijklmnopqrstuvwxyz012345")
    assert "sk_test_" not in out
    assert "[redacted:secret-key]" in out


def test_redacts_cvc() -> None:
    assert "[redacted:cvc]" in redact_text("cvc: 123")
    assert "[redacted:cvc]" in redact_text('"cvc":"999"')
