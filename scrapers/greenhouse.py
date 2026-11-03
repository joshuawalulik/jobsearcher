"""Greenhouse ATS adapter: fetch a board's job list and normalize postings."""

import html

from . import common

ATS_TYPE = "greenhouse"


def fetch(slug: str, *, user_agent: str, timeout: float = 10.0):
    """GET the Greenhouse board API and return the raw parsed payload."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    return common.fetch_json(url, user_agent=user_agent, timeout=timeout)


def normalize(raw, slug: str | None = None) -> list[dict]:
    """Raw Greenhouse payload → list of normalized posting dicts. Pure, no I/O.

    slug is unused (kept for the adapter-uniform signature): links go
    straight to absolute_url, which for some boards is the company's own
    careers page. Those occasionally sit behind twitchy WAFs; the card's
    on-page description covers reading the JD when a page won't load.
    """
    jobs = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(jobs, list):
        raise common.FetchError("greenhouse payload has no 'jobs' list")
    postings: list[dict] = []
    for job in jobs:
        if not isinstance(job, dict):
            print(f"greenhouse: skipping non-dict posting: {job!r:.120}")
            continue
        job_id = job.get("id")
        title = job.get("title")
        if not job_id or not title:
            print(f"greenhouse: skipping posting with missing id/title: id={job_id!r} title={title!r}")
            continue
        location = job.get("location") or {}
        departments = job.get("departments") or []
        first_dept = departments[0] if departments else None
        # Greenhouse `content` is entity-escaped HTML: unescape once, then strip.
        content = job.get("content")
        postings.append(
            {
                "external_id": str(job_id),
                "title": title,
                "location": location.get("name"),
                "department": first_dept.get("name") if isinstance(first_dept, dict) else None,
                "url": job.get("absolute_url"),
                "description": common.html_to_text(html.unescape(content)) if content else None,
                "posted_at": common.iso_to_utc_string(
                    job.get("first_published") or job.get("updated_at")
                ),
            }
        )
    return postings
