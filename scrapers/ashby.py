"""Ashby ATS adapter: fetch a board's postings and normalize them to job dicts."""

from scrapers import common

ATS_TYPE = "ashby"


def fetch(slug: str, *, user_agent: str, timeout: float = 10.0):
    """Fetch the raw job-board payload for an Ashby board ({"jobs": [...]})."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    raw = common.fetch_json(url, user_agent=user_agent, timeout=timeout)
    # Ashby can return 200 with an error-shaped body for unknown boards.
    if isinstance(raw, dict) and "jobs" not in raw:
        raise common.BoardNotFound(f"no 'jobs' key in Ashby payload for {slug!r}")
    return raw


def normalize(raw, slug: str | None = None) -> list[dict]:  # slug unused: jobUrl is ATS-hosted
    """Pure: raw Ashby payload ({"jobs": [...]}) → list of normalized job dicts."""
    postings = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(postings, list):
        raise common.FetchError(
            f"unexpected Ashby payload: expected a dict with a 'jobs' list, got {raw!r:.120}"
        )
    jobs: list[dict] = []
    for posting in postings:
        if not isinstance(posting, dict):
            print(f"warning: skipping non-dict Ashby posting: {posting!r:.120}")
            continue
        if posting.get("isListed") is False:
            continue
        external_id = posting.get("id")
        title = posting.get("title")
        if not external_id or not title:
            print(f"warning: skipping Ashby posting missing id or title: {posting!r:.120}")
            continue
        jobs.append(
            {
                "external_id": external_id,
                "title": title,
                "location": posting.get("location"),
                "department": posting.get("department"),
                "url": posting.get("jobUrl"),
                "description": posting.get("descriptionPlain")
                or common.html_to_text(posting.get("descriptionHtml")),
                "posted_at": common.iso_to_utc_string(posting.get("publishedAt")),
            }
        )
    return jobs
