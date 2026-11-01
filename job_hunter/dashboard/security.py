"""Security helpers and middleware for the local-only dashboard.

Protections:
- Reject any request whose `Host` header is not `127.0.0.1` or `localhost` (DNS rebinding guard).
- Reject state-changing requests (`POST`/`PUT`/`PATCH`/`DELETE`) whose `Origin` / `Referer` /
  `Sec-Fetch-Site` indicates a non-localhost or cross-site caller (CSRF guard).
- Sanitize scraped job URLs so only `http://` and `https://` links are ever rendered.
- Attach strict `Content-Security-Policy` and anti-framing/sniffing headers to every response.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost"})
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def is_localhost_host_header(host: str | None) -> bool:
    """Return True iff `Host` is `127.0.0.1[:port]` or `localhost[:port]`."""
    if not host:
        return False
    candidate = host.strip()
    if not candidate or _has_control_chars(candidate) or any(ch.isspace() for ch in candidate):
        return False
    try:
        parsed = urlsplit(f"http://{candidate}")
        _ = parsed.port
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path or parsed.query or parsed.fragment:
        return False
    return (parsed.hostname or "").lower() in LOCALHOST_HOSTS


def is_localhost_origin(origin_or_referer: str | None) -> bool:
    """Return True iff an `Origin` or `Referer` URL has http(s) scheme and localhost host."""
    if not origin_or_referer:
        return False
    candidate = origin_or_referer.strip()
    if not candidate or candidate.lower() == "null" or _has_control_chars(candidate):
        return False
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    return (parsed.hostname or "").lower() in LOCALHOST_HOSTS


def safe_external_url(url: str | None) -> str | None:
    """Allow only valid `http://` or `https://` URLs with a non-empty hostname."""
    if not url:
        return None
    candidate = url.strip()
    if not candidate or _has_control_chars(candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if not parsed.netloc or not parsed.hostname:
        return None
    return candidate


def safe_local_redirect(target: str | None, fallback: str = "/") -> str:
    """Ensure redirect targets stay on the local app path (never external or protocol-relative)."""
    if not target:
        return fallback
    candidate = target.strip()
    if not candidate or _has_control_chars(candidate):
        return fallback
    if not candidate.startswith("/") or candidate.startswith("//") or candidate.startswith("/\\"):
        return fallback
    return candidate


class LocalhostSecurityMiddleware:
    """Pure ASGI middleware enforcing localhost Host/Origin rules and security headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        host = request.headers.get("host")
        if not is_localhost_host_header(host):
            resp = PlainTextResponse("Forbidden: Host must be localhost (127.0.0.1 or localhost).", status_code=403)
            await resp(scope, receive, send)
            return

        if request.method.upper() in STATE_CHANGING_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                if not is_localhost_origin(origin):
                    resp = PlainTextResponse("Forbidden: Origin must be localhost.", status_code=403)
                    await resp(scope, receive, send)
                    return
            else:
                referer = request.headers.get("referer")
                if referer is not None and not is_localhost_origin(referer):
                    resp = PlainTextResponse("Forbidden: Referer must be localhost.", status_code=403)
                    await resp(scope, receive, send)
                    return

            sec_site = request.headers.get("sec-fetch-site")
            if sec_site is not None and sec_site.strip().lower() not in ("same-origin", "none"):
                resp = PlainTextResponse("Forbidden: Cross-site state-changing request rejected.", status_code=403)
                await resp(scope, receive, send)
                return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", CSP_POLICY)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
            await send(message)

        await self.app(scope, receive, send_with_headers)
