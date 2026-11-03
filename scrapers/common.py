"""Shared scraper helpers: HTTP fetch, raw-response cache, HTML→text, timestamps."""

import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import httpx

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
UTC_FORMAT = "%Y-%m-%d %H:%M:%S"  # matches SQLite datetime('now')


class FetchError(Exception):
    """Network failure, HTTP error, or malformed JSON from a board API."""


class BoardNotFound(FetchError):
    """The board returned 404 — usually a wrong slug in profile.yaml."""


def fetch_json(url: str, *, user_agent: str, timeout: float = 10.0):
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise FetchError(f"request failed: {exc}") from exc
    if response.status_code == 404:
        raise BoardNotFound(f"404 from {url}")
    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code} from {url}")
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"malformed JSON from {url}: {exc}") from exc


def write_cache(ats_type: str, slug: str, raw) -> Path:
    """Cache the latest raw response, one file per board, overwritten each sweep."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{ats_type}_{slug}.json"
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    return path


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html_str: str | None) -> str | None:
    """Strip tags, decode entities once, collapse whitespace. None/empty → None.

    Input must be ordinary HTML. Entity-escaped HTML (Greenhouse `content`)
    needs one html.unescape() by the caller first — doing it here would
    double-unescape and delete literal <angle-bracketed> text from
    already-plain HTML.
    """
    if not html_str:
        return None
    parser = _TextExtractor()
    parser.feed(html_str)
    text = " ".join(" ".join(parser.parts).split())
    return text or None


def iso_to_utc_string(value: str | None) -> str | None:
    """ISO 8601 (with offset or trailing Z) → 'YYYY-MM-DD HH:MM:SS' UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime(UTC_FORMAT)


def epoch_ms_to_utc_string(value) -> str | None:
    """Epoch milliseconds → 'YYYY-MM-DD HH:MM:SS' UTC."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).strftime(UTC_FORMAT)
    except (TypeError, ValueError, OSError):
        return None
