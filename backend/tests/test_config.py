import pytest

from intentguard.config import get_settings


def test_defaults_to_sandbox(monkeypatch):
    monkeypatch.delenv("PAYPAL_ENVIRONMENT", raising=False)
    assert get_settings().paypal_api_base == "https://api-m.sandbox.paypal.com"


def test_refuses_production(monkeypatch):
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "PRODUCTION")
    with pytest.raises(RuntimeError, match="SANDBOX"):
        get_settings()
