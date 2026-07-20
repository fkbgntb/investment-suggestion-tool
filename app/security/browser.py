"""Small local-browser CSRF and mutation-rate boundaries."""

from __future__ import annotations

import secrets
from collections import defaultdict, deque
from hmac import compare_digest
from time import monotonic

from fastapi import HTTPException, Request, Response, status

from app.security.local_access import require_local_access

CSRF_COOKIE = "investment_csrf"
CSRF_HEADER = "x-investment-csrf"


class MutationRateLimiter:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(
        self,
        request: Request,
        action: str,
        *,
        limit: int = 3,
        window_seconds: int = 60,
    ) -> None:
        host = request.client.host if request.client is not None else "unknown"
        key = (host, action)
        now = monotonic()
        events = self._events[key]
        while events and events[0] <= now - window_seconds:
            events.popleft()
        if len(events) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="local action rate limit reached; retry later",
            )
        events.append(now)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        request.app.state.csrf_token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=8 * 60 * 60,
        path="/",
    )


def require_csrf(request: Request) -> None:
    require_local_access(request)
    expected: str = request.app.state.csrf_token
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    if (
        not cookie
        or not header
        or not compare_digest(cookie, expected)
        or not compare_digest(header, expected)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")


def rate_limit(request: Request, action: str) -> None:
    limiter: MutationRateLimiter = request.app.state.mutation_rate_limiter
    limiter.check(request, action)
