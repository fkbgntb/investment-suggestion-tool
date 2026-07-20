import pytest
from pydantic import SecretStr, ValidationError

from app.collectors.factory import build_safe_http_client
from app.config import Settings


def test_defaults_are_local_and_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.debug is False
    assert settings.allow_public_bind is False
    assert settings.deepseek_api_key is None


def test_non_loopback_binding_requires_explicit_acknowledgement() -> None:
    with pytest.raises(ValidationError, match="non-loopback binding"):
        Settings(_env_file=None, host="0.0.0.0")  # noqa: S104 - deliberate security test

    settings = Settings(
        _env_file=None,
        host="0.0.0.0",  # noqa: S104 - deliberate security test
        allow_public_bind=True,
    )
    assert settings.host == "0.0.0.0"  # noqa: S104 - deliberate security test


def test_debug_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="debug mode"):
        Settings(_env_file=None, environment="production", debug=True)


def test_deepseek_key_is_stored_as_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret-value")
    settings = Settings(_env_file=None)

    assert isinstance(settings.deepseek_api_key, SecretStr)
    assert "test-only-secret-value" not in repr(settings.deepseek_api_key)


def test_collector_proxy_is_optional_and_typed() -> None:
    direct = Settings(_env_file=None)
    proxied = Settings(_env_file=None, collector_proxy_url="http://127.0.0.1:7897")

    assert direct.collector_proxy_url is None
    assert str(proxied.collector_proxy_url) == "http://127.0.0.1:7897/"
    assert build_safe_http_client(direct)._proxy_url is None
    assert build_safe_http_client(proxied)._proxy_url == "http://127.0.0.1:7897/"
