"""Resolve a pasted ATS job URL to one normalized posting via JSON APIs.

Powers the tracker's fetch-from-URL flow. Recognizes Greenhouse, Lever
and Ashby posting URLs only — per the hard rules this stays on JSON
APIs, so company marketing pages and LinkedIn links are declined with a
clear error instead of half-parsed HTML.
"""

import re
from urllib.parse import unquote

from scrapers import ashby, common, greenhouse, lever

# (ats_type, pattern with groups (slug, external_id))
_PATTERNS = [
    # job-boards.greenhouse.io/{slug}/jobs/{id} (also boards., .eu variants)
    ("greenhouse", re.compile(
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?!embed/)([^/?#]+)/jobs/(\d+)"
    )),
    # boards.greenhouse.io/embed/job_app?for={slug}&token={id}
    ("greenhouse", re.compile(
        r"greenhouse\.io/embed/job_app\?(?=[^#]*\bfor=([^&#]+))(?=[^#]*\btoken=(\d+))"
    )),
    ("lever", re.compile(r"jobs(?:\.eu)?\.lever\.co/([^/?#]+)/([0-9a-fA-F-]{36})")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]{36})")),
]


def parse_job_url(url: str) -> tuple[str, str, str] | None:
    """(ats_type, slug, external_id) for a recognized posting URL, else None."""
    for ats_type, pattern in _PATTERNS:
        if match := pattern.search(url):
            return ats_type, unquote(match.group(1)), match.group(2)
    return None


def fetch_posting(
    ats_type: str, slug: str, external_id: str, *, user_agent: str, timeout: float
) -> dict | None:
    """One normalized job dict for the posting, or None if the board no
    longer lists it. Raises common.FetchError on network/payload trouble.

    Greenhouse and Lever have single-posting endpoints; Ashby only serves
    whole boards, so its posting is picked out of the board payload. All
    responses go through the adapters' normalize() for consistent shape
    (including the greenhouse ATS-hosted URL policy).
    """
    if ats_type == "greenhouse":
        raw = common.fetch_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{external_id}",
            user_agent=user_agent,
            timeout=timeout,
        )
        jobs = greenhouse.normalize({"jobs": [raw]}, slug)
    elif ats_type == "lever":
        raw = common.fetch_json(
            f"https://api.lever.co/v0/postings/{slug}/{external_id}?mode=json",
            user_agent=user_agent,
            timeout=timeout,
        )
        jobs = lever.normalize([raw])
    elif ats_type == "ashby":
        raw = ashby.fetch(slug, user_agent=user_agent, timeout=timeout)
        jobs = [j for j in ashby.normalize(raw) if j["external_id"] == external_id]
    else:
        raise ValueError(f"unknown ats_type {ats_type!r}")
    return jobs[0] if jobs else None
