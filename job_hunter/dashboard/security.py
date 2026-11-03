"""Security helpers and middleware for the local-only dashboard.

Protections:
- Reject any request whose `Host` header is not `127.0.0.1` or `localhost` (DNS rebinding guard).
- Reject state-changing requests (`POST`/`PUT`/`PATCH`/`DELETE`) whose `Origin` / `Referer` /
  `Sec-Fetch-Site` indicates a non-localhost or cross-site caller.
- Enforce signed `HttpOnly; SameSite=Strict` cookie + form/header CSRF token on all `POST` endpoints.
- Sanitize scraped job URLs so only `http://` and `https://` links are ever rendered.
- Attach strict `Content-Security-Policy` and anti-framing/sniffing headers to every response.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost"})
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_COOKIE_NAME = "jh_csrf"

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class CsrfError(Exception):
    """Raised when a state-changing request is missing or fails CSRF token validation."""


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def generate_csrf_cookie(secret: bytes) -> tuple[str, str]:
    """Return `(raw_token, signed_cookie_value)` using HMAC-SHA256."""
    token = secrets.token_urlsafe(24)
    sig = hmac.new(secret, token.encode("ascii"), hashlib.sha256).hexdigest()
    return token, f"{token}.{sig}"


def verify_csrf_cookie(cookie_value: str | None, secret: bytes) -> str | None:
    """Verify `signed_cookie_value` against `secret`; return `raw_token` if valid, else None."""
    if not cookie_value or "." not in cookie_value:
        return None
    token, sig = cookie_value.split(".", 1)
    if len(token) < 16 or _has_control_chars(token) or _has_control_chars(sig):
        return None
    try:
        expected = hmac.new(secret, token.encode("ascii"), hashlib.sha256).hexdigest()
    except UnicodeEncodeError:
        return None
    if not hmac.compare_digest(sig, expected):
        return None
    return token


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency enforcing signed Double-Submit CSRF token on POST routes."""
    secret: bytes = request.app.state.csrf_secret
    cookie_token = verify_csrf_cookie(request.cookies.get(CSRF_COOKIE_NAME), secret)
    if not cookie_token:
        raise CsrfError("Forbidden: Missing or invalid CSRF cookie.")

    submitted = request.headers.get("X-CSRF-Token")
    if submitted is None:
        form = await request.form()
        raw_field = form.get("csrf_token")
        submitted = raw_field if isinstance(raw_field, str) else None

    if not submitted or not secrets.compare_digest(submitted.strip(), cookie_token):
        raise CsrfError("Forbidden: Invalid or missing CSRF token.")


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
    """Pure ASGI middleware enforcing localhost Host/Origin rules, CSRF cookies, and security headers."""

    def __init__(self, app: ASGIApp, *, csrf_secret: bytes | None = None) -> None:
        self.app = app
        self.csrf_secret = csrf_secret or secrets.token_bytes(32)

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

        method = request.method.upper()
        if method in STATE_CHANGING_METHODS:
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

        cookie_token = verify_csrf_cookie(request.cookies.get(CSRF_COOKIE_NAME), self.csrf_secret)
        set_csrf_cookie_val: str | None = None
        if cookie_token is None and method not in STATE_CHANGING_METHODS:
            cookie_token, set_csrf_cookie_val = generate_csrf_cookie(self.csrf_secret)

        scope.setdefault("state", {})["csrf_token"] = cookie_token or ""

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", CSP_POLICY)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                if set_csrf_cookie_val is not None:
                    headers.append(
                        "set-cookie",
                        f"{CSRF_COOKIE_NAME}={set_csrf_cookie_val}; Path=/; HttpOnly; SameSite=Strict",
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)
