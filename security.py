"""Request checks and response headers, on for every run of the app.

The app has one user, but the browser it runs in has every other site open
too. So each request is checked for which host it asked for, where it came
from and how big it is, and each response says what the page may load.

The rules are pure functions (refusal, form_problem, clean_url) so they can
be tested without a server; Guard is the thin ASGI layer that applies them.
The limits are constants here: mechanics, not preferences.
"""

import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The app answers to these names only. A page on another site that points
# its own hostname at 127.0.0.1 (DNS rebinding) arrives with that hostname
# in Host, and is turned away. A deployment adds its hostname with the
# environment variable, comma-separated.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
HOSTS_ENV = "JOBSEARCHER_ALLOWED_HOSTS"

MAX_BODY_BYTES = 256 * 1024
MAX_QUERY_BYTES = 2048
MAX_FORM_FIELDS = 100
MAX_FIELD_CHARS = 1_000
# Fields that hold real text. Everything else gets MAX_FIELD_CHARS.
FIELD_CHARS = {
    "description": 100_000,
    "notes": 50_000,
    "url": 2_000,
    "linkedin_url": 2_000,
    "next_action": 2_000,
    "next_step": 2_000,
    "ask": 2_000,
    "summary": 2_000,
    "note": 2_000,
}
# The profile editor posts whole lists (keywords, the watchlist) per field.
PATH_FIELD_CHARS = {"/profile": 50_000}

UNKNOWN_HOST = "Unknown host."
CROSS_SITE = "Cross-site request refused."
TOO_BIG = "Request too large."
QUERY_TOO_LONG = "Address too long."

CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))
RESPONSE_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # same-origin, not no-referrer: with no-referrer a browser sends
    # "Origin: null" on a plain form post, which the cross-site check
    # would have to refuse. Other sites still get no referrer either way.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# A refusal or an error is never worth keeping: a cache between the app and
# the browser must not hand one visitor's to the next.
REFUSAL_HEADERS = {**RESPONSE_HEADERS, "Cache-Control": "no-store"}

_FIELD_NAME = re.compile(r"[\w.-]{1,60}")
_HOST = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::\d{1,5})?")


def allowed_hosts() -> frozenset[str]:
    extra = os.environ.get(HOSTS_ENV, "")
    return LOCAL_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


def host_name(host: str | None) -> str:
    """The Host header without its port, lowercased. '' if it isn't a
    plain name or bracketed IPv6 address with an optional numeric port."""
    match = _HOST.fullmatch((host or "").strip().lower())
    return match[1] if match else ""


def host_allowed(host: str | None) -> bool:
    return host_name(host) in allowed_hosts()


def cross_site(method: str, headers: Headers) -> bool:
    """True when another site's page is behind this request.

    Browsers say where a request came from in Sec-Fetch-Site, and older
    ones in Origin. A request with neither isn't from a page at all (curl,
    a script, the launcher's health check), so it can't be a forged one.
    """
    site = headers.get("sec-fetch-site")
    if method.upper() in SAFE_METHODS:
        if site in (None, "same-origin", "none"):
            return False
        # A link into the app from elsewhere is fine. Another site loading
        # a page here as an image, a script, a frame or a fetch is not.
        return not (
            headers.get("sec-fetch-mode") == "navigate"
            and headers.get("sec-fetch-dest") == "document"
        )
    if site is not None:
        return site not in ("same-origin", "none")
    origin = headers.get("origin")
    if origin is None:
        return False
    return urlsplit(origin).netloc.lower() != (headers.get("host") or "").strip().lower()


def refusal(method: str, headers: Headers, query_string: bytes = b"") -> tuple[int, str] | None:
    """(status, message) for a request to turn away before the app sees it."""
    if not host_allowed(headers.get("host")):
        return 400, UNKNOWN_HOST
    if len(query_string) > MAX_QUERY_BYTES:
        return 414, QUERY_TOO_LONG
    if cross_site(method, headers):
        return 403, CROSS_SITE
    declared = headers.get("content-length")
    if declared is not None:
        if not (declared.isascii() and declared.isdigit()):
            return 400, "Bad Content-Length."
        if int(declared) > MAX_BODY_BYTES:
            return 413, TOO_BIG
    return None


def field_limit(path: str, name: str) -> int:
    return PATH_FIELD_CHARS.get(path) or FIELD_CHARS.get(name, MAX_FIELD_CHARS)


def form_problem(path: str, items: list[tuple[str, object]]) -> str | None:
    """What's wrong with a submitted form's size or shape, or None.

    The message never repeats a submitted value, and names a field only
    when the name is an ordinary one.
    """
    if len(items) > MAX_FORM_FIELDS:
        return "Too many form fields."
    for name, value in items:
        label = name if _FIELD_NAME.fullmatch(name) else "A field"
        if not isinstance(value, str):
            return "File uploads aren't accepted."
        limit = field_limit(path, name)
        if len(value) > limit:
            return f"{label} is too long (the limit is {limit:,} characters)."
    return None


def clean_url(value: str | None) -> str | None:
    """A link as an http(s) address, or None if it can't be one.

    Stored URLs are rendered as links, so a javascript: or data: address
    must never be saved or shown. An address typed without a scheme gets
    https://, as the add-job form always did.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if "://" not in value:
        value = f"https://{value}"
    try:
        parts = urlsplit(value)
    except ValueError:  # a malformed address, such as an unclosed [ in the host
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return value


class Guard:
    """ASGI middleware: turn away bad requests, cap bodies, add the headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        refused = refusal(
            scope["method"], Headers(scope=scope), scope.get("query_string", b"")
        )
        if refused is not None:
            status, message = refused
            response = PlainTextResponse(message, status_code=status, headers=REFUSAL_HEADERS)
            await response(scope, receive, send)
            return

        received = 0
        too_big = False
        refused_sent = False

        async def capped_receive():
            # Content-Length was checked above; this catches a body sent in
            # chunks with no length declared.
            nonlocal received, too_big
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    too_big = True
                    raise HTTPException(status_code=413, detail=TOO_BIG)
            return message

        async def send_with_headers(message):
            nonlocal refused_sent
            if too_big:
                # Whatever the app made of the interrupted read (other
                # middleware can wrap the error into a 400), the answer is
                # the same: too large. Sent once, in place of the app's.
                if not refused_sent:
                    refused_sent = True
                    response = PlainTextResponse(
                        TOO_BIG, status_code=413, headers=REFUSAL_HEADERS
                    )
                    await response(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in RESPONSE_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, capped_receive, send_with_headers)
