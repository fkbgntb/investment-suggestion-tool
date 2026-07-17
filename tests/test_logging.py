from app.logging import redact_sensitive


def test_labeled_secrets_are_redacted() -> None:
    message = "authorization: Bearer abc123; api_key=secret-value, cookie=session=value"
    redacted = redact_sensitive(message)

    assert "abc123" not in redacted
    assert "secret-value" not in redacted
    assert "session=value" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_token_like_secrets_are_redacted() -> None:
    message = "provider returned ds-1234567890abcdef"
    assert redact_sensitive(message) == "provider returned [REDACTED]"
