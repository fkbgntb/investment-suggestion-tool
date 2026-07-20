"""Construct collection infrastructure from typed application settings."""

from __future__ import annotations

from app.collectors.safe_http import SafeHTTPClient
from app.config import Settings


def build_safe_http_client(settings: Settings) -> SafeHTTPClient:
    proxy_url = str(settings.collector_proxy_url) if settings.collector_proxy_url else None
    return SafeHTTPClient(proxy_url=proxy_url)
