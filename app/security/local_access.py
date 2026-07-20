"""Reusable loopback-only boundary for private local administration APIs."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from app.config import Settings


def is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().removeprefix("[").removesuffix("]")
    if normalized.casefold() == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def require_local_access(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not is_loopback_host(settings.host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="private local APIs are disabled on a public bind address",
        )
    client_host = request.client.host if request.client is not None else None
    if not is_loopback_host(client_host):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local access only")
    origin = request.headers.get("origin")
    if origin is not None and not is_loopback_host(urlsplit(origin).hostname):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-origin private API access is forbidden",
        )
