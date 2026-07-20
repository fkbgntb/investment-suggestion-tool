"""Low-privilege outbound collection infrastructure."""

from app.collectors.safe_http import SafeHTTPClient, SafeHTTPResponse

__all__ = ["SafeHTTPClient", "SafeHTTPResponse"]
