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


_BLOCK_TAGS = {"div", "br", "ul", "ol", "tr", "table", "section", "article"}
_PARAGRAPH_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}


class _TextExtractor(HTMLParser):
    """Collects text, keeping line breaks where block tags were."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "li":
            self.parts.append("\n\u2022 ")
        elif tag in _PARAGRAPH_TAGS:
            self.parts.append("\n\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _PARAGRAPH_TAGS:
            self.parts.append("\n\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(" ".join(data.split()) if data.strip() else " ")


def html_to_text(html_str: str | None) -> str | None:
    """Strip tags and decode entities once, keeping paragraphs and bullets.

    Runs of spaces collapse and blank lines stay single. None/empty → None.
    Input must be ordinary HTML. Entity-escaped HTML (Greenhouse `content`)
    needs one html.unescape() by the caller first — doing it here would
    double-unescape and delete literal <angle-bracketed> text from
    already-plain HTML.
    """
    if not html_str:
        return None
    parser = _TextExtractor()
    parser.feed(html_str)
    lines = [" ".join(line.split()) for line in " ".join(parser.parts).split("\n")]
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() or None


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
