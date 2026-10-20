import halvorai_ingest


def test_exports_package_name() -> None:
    assert halvorai_ingest.name == "halvorai-ingest"


def test_public_api() -> None:
    assert hasattr(halvorai_ingest, "HalvorClient")
    assert hasattr(halvorai_ingest, "agent_context")
    assert hasattr(halvorai_ingest, "sign_event")
