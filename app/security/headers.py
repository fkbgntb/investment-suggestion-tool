"""Baseline HTTP security headers for local and future hosted use."""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response


def _is_embeddable_report(request: Request, response: Response) -> bool:
    path = request.url.path
    return (
        response.status_code == 200
        and path.startswith("/api/v1/reports/")
        and path.endswith("/html")
    )


async def add_security_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'"
        )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = (
        "SAMEORIGIN" if _is_embeddable_report(request, response) else "DENY"
    )
    response.headers["Cache-Control"] = "no-store"
    return response
