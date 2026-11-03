"""Lever ATS adapter: fetch a board's postings and normalize them to job dicts."""

from scrapers import common

ATS_TYPE = "lever"


def fetch(slug: str, *, user_agent: str, timeout: float = 10.0):
    """Fetch the raw postings payload for a Lever board (a bare JSON list)."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    return common.fetch_json(url, user_agent=user_agent, timeout=timeout)


def _full_description(posting: dict) -> str | None:
    """Stitch the whole JD back together from Lever's split fields.

    descriptionPlain is only the intro — the requirement/responsibility
    bullets live in lists[] (heading `text` + HTML `content`) and the
    closing boilerplate in additionalPlain. Storing just the intro loses
    exactly the sections keyword scoring needs, and is empty for some
    postings.
    """
    parts = [(posting.get("descriptionPlain") or "").strip()]
    for section in posting.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = (section.get("text") or "").strip()
        content = common.html_to_text(section.get("content")) or ""
        parts.append(f"{heading}\n{content}".strip())
    parts.append((posting.get("additionalPlain") or "").strip())
    return "\n\n".join(part for part in parts if part) or None


def normalize(raw, slug: str | None = None) -> list[dict]:  # slug unused: hostedUrl is ATS-hosted
    """Pure: raw Lever payload (list of postings) → list of normalized job dicts."""
    if not isinstance(raw, list):
        raise common.FetchError(
            f"unexpected Lever payload: expected a list, got {type(raw).__name__}"
        )
    jobs: list[dict] = []
    for posting in raw:
        if not isinstance(posting, dict):
            print(f"warning: skipping non-dict Lever posting: {posting!r:.120}")
            continue
        external_id = posting.get("id")
        title = posting.get("text")
        if not external_id or not title:
            print(f"warning: skipping Lever posting missing id or text: {posting!r:.120}")
            continue
        categories = posting.get("categories") or {}
        jobs.append(
            {
                "external_id": external_id,
                "title": title,
                "location": categories.get("location"),
                "department": categories.get("team") or categories.get("department"),
                "url": posting.get("hostedUrl"),
                "description": _full_description(posting),
                "posted_at": common.epoch_ms_to_utc_string(posting.get("createdAt")),
            }
        )
    return jobs
