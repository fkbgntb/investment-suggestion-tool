"""Policy-bound HTTP client for untrusted public information sources."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_address
from time import monotonic
from urllib.parse import urljoin, urlsplit

import httpx

from app.collectors.resolver import DNSResolver, SystemDNSResolver
from app.domain.collection import FetchFailure, SourceHealthSnapshot, URLPolicy
from app.domain.enums import FetchErrorCode, SourceHealthStatus

logger = logging.getLogger(__name__)

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class SafeHTTPResponse:
    source_id: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes


@dataclass
class _SourceState:
    consecutive_failures: int = 0
    last_error_code: FetchErrorCode | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    circuit_open_until: datetime | None = None


class SafeFetchError(RuntimeError):
    """Sanitized collection failure that never includes a URL or resolved address."""

    def __init__(
        self,
        source_id: str,
        error_code: FetchErrorCode,
        *,
        retryable: bool,
    ) -> None:
        self.source_id = source_id
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(f"source fetch failed: {source_id} ({error_code.value})")

    def as_failure(self, occurred_at: datetime | None = None) -> FetchFailure:
        return FetchFailure(
            source_id=self.source_id,
            error_code=self.error_code,
            retryable=self.retryable,
            occurred_at=occurred_at or datetime.now(UTC),
        )


class SafeHTTPClient:
    """Fetch only policy-approved public HTTP resources with bounded resource use."""

    def __init__(
        self,
        *,
        resolver: DNSResolver | None = None,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolver = resolver or SystemDNSResolver()
        self._proxy_url = proxy_url
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._states: dict[str, _SourceState] = {}
        self._rate_locks: dict[str, asyncio.Lock] = {}
        self._last_request_at: dict[str, float] = {}

    async def __aenter__(self) -> SafeHTTPClient:
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            proxy=self._proxy_url,
            transport=self._transport,
        )
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str, policy: URLPolicy) -> SafeHTTPResponse:
        self._assert_started(policy.source_id)
        self._assert_circuit_closed(policy)
        await self._wait_for_source_slot(policy)
        try:
            async with asyncio.timeout(policy.total_timeout_seconds):
                result = await self._fetch_with_redirects(url, policy)
        except SafeFetchError as error:
            self._record_failure(policy, error)
            logger.warning(
                "source fetch rejected source_id=%s error_code=%s",
                policy.source_id,
                error.error_code.value,
            )
            raise
        except (TimeoutError, httpx.TimeoutException) as cause:
            error = SafeFetchError(
                policy.source_id,
                FetchErrorCode.TIMEOUT,
                retryable=True,
            )
            self._record_failure(policy, error)
            logger.warning(
                "source fetch failed source_id=%s error_code=%s",
                policy.source_id,
                error.error_code.value,
            )
            raise error from cause
        except httpx.RequestError as cause:
            error = SafeFetchError(
                policy.source_id,
                FetchErrorCode.NETWORK_ERROR,
                retryable=True,
            )
            self._record_failure(policy, error)
            logger.warning(
                "source fetch failed source_id=%s error_code=%s",
                policy.source_id,
                error.error_code.value,
            )
            raise error from cause

        state = self._state(policy.source_id)
        state.consecutive_failures = 0
        state.last_error_code = None
        state.last_success_at = datetime.now(UTC)
        state.circuit_open_until = None
        return result

    def health(self, source_id: str) -> SourceHealthSnapshot:
        state = self._state(source_id)
        now = datetime.now(UTC)
        circuit_is_open = state.circuit_open_until is not None and state.circuit_open_until > now
        if circuit_is_open:
            status = SourceHealthStatus.CIRCUIT_OPEN
        elif state.consecutive_failures:
            status = SourceHealthStatus.DEGRADED
        else:
            status = SourceHealthStatus.HEALTHY
        return SourceHealthSnapshot(
            source_id=source_id,
            status=status,
            consecutive_failures=state.consecutive_failures,
            last_error_code=state.last_error_code,
            last_success_at=state.last_success_at,
            last_failure_at=state.last_failure_at,
            circuit_open_until=state.circuit_open_until if circuit_is_open else None,
        )

    async def _fetch_with_redirects(self, url: str, policy: URLPolicy) -> SafeHTTPResponse:
        current_url = url
        redirects = 0
        while True:
            await self._validate_url(current_url, policy)
            client = self._client
            assert client is not None
            timeout = httpx.Timeout(
                connect=policy.connect_timeout_seconds,
                read=policy.read_timeout_seconds,
                write=policy.connect_timeout_seconds,
                pool=policy.connect_timeout_seconds,
            )
            async with client.stream(
                "GET",
                current_url,
                headers={"User-Agent": policy.user_agent, "Accept": "*/*"},
                timeout=timeout,
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    if redirects >= policy.max_redirects:
                        raise SafeFetchError(
                            policy.source_id,
                            FetchErrorCode.REDIRECT_LIMIT,
                            retryable=False,
                        )
                    location = response.headers.get("location")
                    if not location:
                        raise SafeFetchError(
                            policy.source_id,
                            FetchErrorCode.REDIRECT_REJECTED,
                            retryable=False,
                        )
                    current_url = urljoin(current_url, location)
                    redirects += 1
                    continue

                if response.status_code >= 400 or 300 <= response.status_code < 400:
                    raise SafeFetchError(
                        policy.source_id,
                        FetchErrorCode.HTTP_STATUS,
                        retryable=response.status_code >= 500 or response.status_code == 429,
                    )

                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                content_type = content_type.casefold()
                if content_type not in policy.allowed_content_types:
                    raise SafeFetchError(
                        policy.source_id,
                        FetchErrorCode.CONTENT_TYPE_REJECTED,
                        retryable=False,
                    )
                self._check_content_length(response, policy)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > policy.max_response_bytes:
                        raise SafeFetchError(
                            policy.source_id,
                            FetchErrorCode.RESPONSE_TOO_LARGE,
                            retryable=False,
                        )
                return SafeHTTPResponse(
                    source_id=policy.source_id,
                    final_url=current_url,
                    status_code=response.status_code,
                    content_type=content_type,
                    body=bytes(body),
                )

    async def _validate_url(self, url: str, policy: URLPolicy) -> None:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as cause:
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.INVALID_URL,
                retryable=False,
            ) from cause
        if not parsed.scheme or hostname is None or parsed.username or parsed.password:
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.INVALID_URL,
                retryable=False,
            )
        scheme = parsed.scheme.casefold()
        if scheme not in policy.allowed_schemes:
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.SCHEME_REJECTED,
                retryable=False,
            )
        try:
            normalized_host = hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        except UnicodeError as cause:
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.INVALID_URL,
                retryable=False,
            ) from cause
        if not self._host_allowed(normalized_host, policy):
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.HOST_REJECTED,
                retryable=False,
            )
        effective_port = port or (443 if scheme == "https" else 80)
        if effective_port not in policy.allowed_ports:
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.PORT_REJECTED,
                retryable=False,
            )

        try:
            literal = ip_address(normalized_host)
            addresses = (str(literal),)
        except ValueError:
            try:
                addresses = await self._resolver.resolve(normalized_host, effective_port)
            except (OSError, UnicodeError) as cause:
                raise SafeFetchError(
                    policy.source_id,
                    FetchErrorCode.DNS_FAILED,
                    retryable=True,
                ) from cause
        if not addresses:
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.DNS_FAILED,
                retryable=True,
            )
        for value in addresses:
            try:
                address = ip_address(value)
            except ValueError as cause:
                raise SafeFetchError(
                    policy.source_id,
                    FetchErrorCode.DNS_FAILED,
                    retryable=True,
                ) from cause
            if not self._is_public_address(address):
                raise SafeFetchError(
                    policy.source_id,
                    FetchErrorCode.ADDRESS_REJECTED,
                    retryable=False,
                )

    @staticmethod
    def _host_allowed(hostname: str, policy: URLPolicy) -> bool:
        return any(
            hostname == allowed or (policy.allow_subdomains and hostname.endswith(f".{allowed}"))
            for allowed in policy.allowed_hosts
        )

    @staticmethod
    def _is_public_address(address: IPv4Address | IPv6Address) -> bool:
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return address.is_global and not (
            address.is_multicast or address.is_reserved or address.is_unspecified
        )

    @staticmethod
    def _check_content_length(response: httpx.Response, policy: URLPolicy) -> None:
        value = response.headers.get("content-length")
        if value is None:
            return
        try:
            declared_length = int(value)
        except ValueError:
            return
        if declared_length > policy.max_response_bytes:
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.RESPONSE_TOO_LARGE,
                retryable=False,
            )

    async def _wait_for_source_slot(self, policy: URLPolicy) -> None:
        lock = self._rate_locks.setdefault(policy.source_id, asyncio.Lock())
        async with lock:
            previous = self._last_request_at.get(policy.source_id)
            if previous is not None:
                remaining = policy.minimum_interval_seconds - (monotonic() - previous)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_request_at[policy.source_id] = monotonic()

    def _assert_started(self, source_id: str) -> None:
        if self._client is None:
            raise SafeFetchError(
                source_id,
                FetchErrorCode.NETWORK_ERROR,
                retryable=False,
            )

    def _assert_circuit_closed(self, policy: URLPolicy) -> None:
        state = self._state(policy.source_id)
        now = datetime.now(UTC)
        if state.circuit_open_until is not None and state.circuit_open_until > now:
            raise SafeFetchError(
                policy.source_id,
                FetchErrorCode.CIRCUIT_OPEN,
                retryable=True,
            )

    def _record_failure(self, policy: URLPolicy, error: SafeFetchError) -> None:
        state = self._state(policy.source_id)
        state.consecutive_failures += 1
        state.last_error_code = error.error_code
        state.last_failure_at = datetime.now(UTC)
        if error.retryable and state.consecutive_failures >= policy.circuit_failure_threshold:
            state.circuit_open_until = state.last_failure_at + timedelta(
                seconds=policy.circuit_cooldown_seconds
            )

    def _state(self, source_id: str) -> _SourceState:
        return self._states.setdefault(source_id, _SourceState())
