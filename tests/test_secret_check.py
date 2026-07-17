from scripts.check_secrets import detect_secret_kinds


def test_secret_scanner_warns_for_a_test_provider_key() -> None:
    simulated_key = "sk-" + ("a" * 32)
    assert detect_secret_kinds(simulated_key) == {"provider API token"}


def test_secret_scanner_allows_placeholders_and_short_test_values() -> None:
    text = "DEEPSEEK_API_KEY=你的本机密钥; api_key=secret-value"
    assert detect_secret_kinds(text) == set()
