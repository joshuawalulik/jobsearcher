"""Word-boundary keyword matching shared by gates.py and score.py.

Profile keywords include awkward tokens: "ai" must not match "maintain",
"java" must not match "javascript", "rest" must not match "restaurant",
while ".net", "c++", and "ci/cd" contain regex metacharacters. A boundary
means "not adjacent to an alphanumeric", and is only enforced on a side
where the keyword itself starts/ends with an alphanumeric — so ".net"
still matches "ASP.NET" and "c++" matches "C++17"-free text without the
punctuation blocking the boundary check.

Whitespace in a keyword also matches hyphens, so "full stack" matches
"full-stack engineer".
"""

import re
from functools import lru_cache


@lru_cache(maxsize=1024)
def keyword_pattern(keyword: str) -> re.Pattern:
    """Compile a case-insensitive word-boundary pattern for one keyword."""
    body = r"[\s\-]+".join(re.escape(part) for part in keyword.split())
    prefix = r"(?<![A-Za-z0-9])" if keyword[:1].isalnum() else ""
    suffix = r"(?![A-Za-z0-9])" if keyword[-1:].isalnum() else ""
    return re.compile(f"{prefix}{body}{suffix}", re.IGNORECASE)


def contains_keyword(text: str, keyword: str) -> bool:
    return bool(keyword_pattern(keyword).search(text))


def matching_keywords(text: str, keywords: list[str]) -> list[str]:
    """Keywords found in text, in the order they appear in the profile."""
    return [kw for kw in keywords if contains_keyword(text, kw)]


# "N+ years ... experience" with at most 60 chars between, not crossing a
# sentence boundary. Ranges ("2-4 years", "2 to 4 years") capture their
# lower bound; decimals ("1.5+ years") are read whole, with a lookbehind
# so the match can never start mid-number (the "5" of "1.5"). The
# lookahead keeps "experience" unconsumed so "2+ years ..., 10+ years ...
# experience" yields every number. Shared by the experience gate, the
# experience score penalty, and seniority detection.
_YEARS_PATTERN = re.compile(
    r"(?<![\d.])(\d{1,2}(?:\.\d{1,2})?)(?:\s*(?:[-–—]|to)\s*\d{1,2}(?:\.\d{1,2})?)?"
    r"\s*\+?\s*(?:years?|yrs?)"
    r"(?=[^.]{0,60}?experience)",
    re.IGNORECASE,
)


def required_experience_years(text: str) -> int | float | None:
    """The lowest years-of-experience bar in the text, or None if absent.

    Postings listing several thresholds are nearly always tiered
    alternative qualifications ("Bachelor's AND 2+ years, OR Master's AND
    10+ years") where the lowest number is the real entry bar. Taking the
    minimum occasionally under-penalizes a posting with multiple genuine
    requirements by a few points; taking the maximum buried good jobs
    under tier language, which is the worse error.

    Whole-number bars come back as int so stored reasons read "5 years",
    not "5.0 years"; fractional bars ("1.5+ years") stay float.
    """
    years = [float(m.group(1)) for m in _YEARS_PATTERN.finditer(text)]
    if not years:
        return None
    lowest = min(years)
    return int(lowest) if lowest.is_integer() else lowest
