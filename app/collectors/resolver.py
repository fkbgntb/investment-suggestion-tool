"""Replaceable DNS resolution used by outbound URL validation."""

from __future__ import annotations

import asyncio
import socket
from typing import Protocol


class DNSResolver(Protocol):
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]: ...


class SystemDNSResolver:
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        results = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return tuple(dict.fromkeys(result[4][0] for result in results))
