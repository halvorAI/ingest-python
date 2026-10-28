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


def test_leaves_digit_runs_inside_hex_base64_and_ids_alone() -> None:
    for text in (
        "0x9632e1311996ebcb38d7a4242424242424242b5beb14f513",
        "c2lnbmF0dXJl4242424242424242AAA=",
        "ord_4242424242424242x",
    ):
        assert redact_text(text) == text


def test_still_finds_a_card_number_in_a_url_path() -> None:
    assert redact_text("https://api.example/r/4242424242424242") == "https://api.example/r/[redacted:pan]"
