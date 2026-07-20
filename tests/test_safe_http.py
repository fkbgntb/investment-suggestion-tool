from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable
from pathlib import Path
from time import monotonic

import httpx
import pytest
from pydantic import ValidationError

from app.collectors.safe_http import SafeFetchError, SafeHTTPClient
from app.domain.collection import URLPolicy
from app.domain.enums import FetchErrorCode, SourceHealthStatus

PUBLIC_IP = "93.184.216.34"


class StaticResolver:
    def __init__(self, addresses: dict[str, tuple[str, ...]] | None = None) -> None:
        self.addresses = addresses or {"example.com": (PUBLIC_IP,)}
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.addresses.get(hostname, ())


def policy(
    source_id: str = "source-test",
    *,
    allowed_hosts: tuple[str, ...] = ("example.com",),
    **changes: object,
) -> URLPolicy:
    values: dict[str, object] = {
        "source_id": source_id,
        "allowed_hosts": allowed_hosts,
        "minimum_interval_seconds": 0,
    }
    values.update(changes)
    return URLPolicy(**values)


def run(value: Awaitable[object]) -> object:
    return asyncio.run(value)


def test_fetches_public_bounded_content_without_executing_it() -> None:
    resolver = StaticResolver()

    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=b"<script>must remain inert text</script>",
                request=request,
            )
        )
        async with SafeHTTPClient(resolver=resolver, transport=transport) as client:
            result = await client.fetch("https://example.com/article", policy())
            assert result.body == b"<script>must remain inert text</script>"
            assert result.content_type == "text/html"
            assert client.health("source-test").status is SourceHealthStatus.HEALTHY

    run(scenario())
    assert resolver.calls == [("example.com", 443)]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("ftp://example.com/file", FetchErrorCode.SCHEME_REJECTED),
        ("https://other.example/file", FetchErrorCode.HOST_REJECTED),
        ("https://example.com:8443/file", FetchErrorCode.PORT_REJECTED),
        ("https://user:pass@example.com/file", FetchErrorCode.INVALID_URL),
    ],
)
def test_rejects_unapproved_url_shapes(url: str, expected: FetchErrorCode) -> None:
    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=StaticResolver(), transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ) as client:
            with pytest.raises(SafeFetchError) as captured:
                await client.fetch(url, policy())
            assert captured.value.error_code is expected

    run(scenario())


@pytest.mark.parametrize(
    "address",
    (
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "ff02::1",
    ),
)
def test_rejects_non_public_dns_results(address: str) -> None:
    resolver = StaticResolver({"example.com": (address,)})

    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=resolver, transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ) as client:
            with pytest.raises(SafeFetchError) as captured:
                await client.fetch("https://example.com", policy())
            assert captured.value.error_code is FetchErrorCode.ADDRESS_REJECTED

    run(scenario())


def test_revalidates_dns_and_blocks_redirect_to_private_address() -> None:
    resolver = StaticResolver(
        {"example.com": (PUBLIC_IP,), "metadata.example": ("169.254.169.254",)}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://metadata.example/latest"},
            request=request,
        )

    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=resolver, transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(SafeFetchError) as captured:
                await client.fetch(
                    "https://example.com/redirect",
                    policy(allowed_hosts=("example.com", "metadata.example")),
                )
            assert captured.value.error_code is FetchErrorCode.ADDRESS_REJECTED

    run(scenario())
    assert resolver.calls == [("example.com", 443), ("metadata.example", 443)]


def test_limits_redirect_count_response_size_and_content_type() -> None:
    def redirects(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/again"}, request=request)

    async def scenario() -> None:
        resolver = StaticResolver()
        async with SafeHTTPClient(
            resolver=resolver, transport=httpx.MockTransport(redirects)
        ) as client:
            with pytest.raises(SafeFetchError) as captured:
                await client.fetch(
                    "https://example.com/start",
                    policy(max_redirects=1),
                )
            assert captured.value.error_code is FetchErrorCode.REDIRECT_LIMIT

        async with SafeHTTPClient(
            resolver=resolver,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "application/octet-stream"},
                    content=b"binary",
                    request=request,
                )
            ),
        ) as client:
            with pytest.raises(SafeFetchError) as captured:
                await client.fetch("https://example.com/file", policy())
            assert captured.value.error_code is FetchErrorCode.CONTENT_TYPE_REJECTED

        async with SafeHTTPClient(
            resolver=resolver,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/plain"},
                    content=b"x" * 1_025,
                    request=request,
                )
            ),
        ) as client:
            with pytest.raises(SafeFetchError) as captured:
                await client.fetch(
                    "https://example.com/large",
                    policy(max_response_bytes=1_024),
                )
            assert captured.value.error_code is FetchErrorCode.RESPONSE_TOO_LARGE

    run(scenario())


def test_timeout_opens_only_that_sources_circuit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slow.example":
            await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"ok",
            request=request,
        )

    resolver = StaticResolver({"slow.example": (PUBLIC_IP,), "healthy.example": (PUBLIC_IP,)})

    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=resolver, transport=httpx.MockTransport(handler)
        ) as client:
            slow_policy = policy(
                "source-slow",
                allowed_hosts=("slow.example",),
                connect_timeout_seconds=0.01,
                read_timeout_seconds=0.01,
                total_timeout_seconds=0.01,
                circuit_failure_threshold=1,
            )
            with pytest.raises(SafeFetchError) as captured:
                await client.fetch("https://slow.example", slow_policy)
            assert captured.value.error_code is FetchErrorCode.TIMEOUT
            assert client.health("source-slow").status is SourceHealthStatus.CIRCUIT_OPEN

            with pytest.raises(SafeFetchError) as circuit:
                await client.fetch("https://slow.example", slow_policy)
            assert circuit.value.error_code is FetchErrorCode.CIRCUIT_OPEN

            healthy = await client.fetch(
                "https://healthy.example",
                policy("source-healthy", allowed_hosts=("healthy.example",)),
            )
            assert healthy.body == b"ok"
            assert client.health("source-healthy").status is SourceHealthStatus.HEALTHY

    run(scenario())


def test_source_rate_limit_is_independent() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                content=b"ok",
                request=request,
            )
        )
        async with SafeHTTPClient(resolver=StaticResolver(), transport=transport) as client:
            limited = policy(minimum_interval_seconds=0.02)
            await client.fetch("https://example.com/one", limited)
            started = monotonic()
            await client.fetch("https://example.com/two", limited)
            assert monotonic() - started >= 0.015

    run(scenario())


def test_failure_and_logs_do_not_expose_url_or_resolved_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_url = "https://example.com/private-path?token=must-not-leak"  # noqa: S105

    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=StaticResolver({"example.com": ("127.0.0.1",)}),
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        ) as client:
            with pytest.raises(SafeFetchError) as captured:
                await client.fetch(secret_url, policy())
            failure = captured.value.as_failure()
            assert failure.error_code is FetchErrorCode.ADDRESS_REJECTED
            assert "url" not in failure.model_dump(mode="json")

    run(scenario())
    assert secret_url not in caplog.text
    assert "127.0.0.1" not in caplog.text
    assert "must-not-leak" not in caplog.text


def test_url_policy_rejects_wildcards_header_injection_and_invalid_timeouts() -> None:
    with pytest.raises(ValidationError, match="without wildcards"):
        policy(allowed_hosts=("*.example.com",))
    with pytest.raises(ValidationError, match="line breaks"):
        policy(user_agent="valid-agent\r\nInjected: true")
    with pytest.raises(ValidationError, match="total timeout"):
        policy(
            connect_timeout_seconds=5,
            read_timeout_seconds=10,
            total_timeout_seconds=2,
        )


def test_application_has_one_outbound_http_implementation() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    direct_http_imports: list[str] = []
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.add(node.module)
        if any(
            module == "httpx"
            or module.startswith("httpx.")
            or module in {"aiohttp", "requests", "urllib.request", "urllib3"}
            for module in modules
        ):
            direct_http_imports.append(path.relative_to(app_root).as_posix())

    assert direct_http_imports == ["collectors/safe_http.py"]
