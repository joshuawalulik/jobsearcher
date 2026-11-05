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

# A years phrase whose "experience" is followed by "with", "using", or
# "working with" is scoped to one technology ("3+ years of experience
# using AWS"), not the role's overall floor. Anchored at the years
# match's end, mirroring _YEARS_PATTERN's lookahead, so it classifies
# the same "experience" occurrence that validated the match.
_TECH_SCOPED_PATTERN = re.compile(
    r"^(?:(?!experience)[^.]){0,60}?experience\s+(?:working\s+)?(?:with|using)\b",
    re.IGNORECASE,
)

# Postings that never say "experience": "6+ years building and shipping web
# products", "Minimum 5 years of software development". Only consulted when
# _YEARS_PATTERN finds nothing, so it can't change a posting that already
# reads. Deliberately strict, because bare "N years" text is usually about
# something else (benefits vesting, "18 years of age", company history, a
# founder's bio): the number must look like a requirement ("N+", a range, or
# a minimum) and the words after it must be about engineering work.
_REQUIREMENT_NUMBER = (
    r"(?<![\d.])(?:"
    r"(\d{1,2}(?:\.\d{1,2})?)(?:\s*(?:[-–—]|to)\s*\d{1,2}(?:\.\d{1,2})?\s*\+?|\s*\+)"
    r"|(?<=minimum )(\d{1,2}(?:\.\d{1,2})?)"
    r"|(?<=minimum of )(\d{1,2}(?:\.\d{1,2})?)"
    r"|(?<=min )(\d{1,2}(?:\.\d{1,2})?)"
    r"|(?<=at least )(\d{1,2}(?:\.\d{1,2})?)"
    r")"
)
_ENGINEERING_WORK = (
    r"software|engineering|development|programming|coding|web|"
    r"back[- ]?end|front[- ]?end|full[- ]?stack"
)
_YEARS_OF_ENGINEERING_PATTERN = re.compile(
    _REQUIREMENT_NUMBER
    + r"\s*(?:years?|yrs?)['’]?\s+(?:"
    # "5+ years building", "5+ years of hands-on software development"
    r"(?:of\s+)?(?:(?:hands[- ]on|professional|production|commercial|industry)\s+)*"
    rf"(?:building|developing|shipping|writing|designing|{_ENGINEERING_WORK})\b"
    # "5+ years in software", "5+ years in a backend role"
    rf"|in\s+(?:an?\s+|the\s+)?(?:(?:professional|production|similar)\s+)*(?:{_ENGINEERING_WORK})\b"
    # "5+ years as a senior software engineer"
    r"|as\s+an?\s+(?:[\w/-]+\s+){0,3}?(?:engineer|developer|programmer)\b"
    # "5+ years working as a developer", "5+ years working in engineering"
    rf"|working\s+(?:as\s+an?\s+(?:[\w/-]+\s+){{0,3}}?(?:engineer|developer|programmer)\b"
    rf"|in\s+(?:an?\s+)?(?:{_ENGINEERING_WORK})\b)"
    r")",
    re.IGNORECASE,
)

# "5+ years building software, with at least 1-2 years on LLM apps": what
# follows "with" narrows the main bar, it doesn't replace it. Without this the
# lowest number would win and a five-year role would read as one. It only
# counts as narrowing when an earlier years figure sits in the same sentence:
# "a degree or equivalent, with 5+ years building software" IS the main bar.
_SUB_REQUIREMENT_LEAD = re.compile(
    r"\b(?:with|including|plus)\s+(?:(?:at\s+least|a\s+minimum\s+of|minimum(?:\s+of)?|min)\s+)?$",
    re.IGNORECASE,
)
_EARLIER_YEARS_IN_SENTENCE = re.compile(
    r"\d\s*\+?\s*(?:years?|yrs?)\b[^.\n]{0,100}$", re.IGNORECASE
)


def _is_sub_requirement(text: str, start: int) -> bool:
    before = text[max(0, start - 130):start]
    return bool(
        _SUB_REQUIREMENT_LEAD.search(before) and _EARLIER_YEARS_IN_SENTENCE.search(before)
    )


def _years_of_engineering(text: str) -> list[float]:
    """Requirement-shaped engineering years, leaving out sub-requirements."""
    return [
        float(next(group for group in m.groups() if group))
        for m in _YEARS_OF_ENGINEERING_PATTERN.finditer(text)
        if not _is_sub_requirement(text, m.start())
    ]


def required_experience_years(text: str) -> int | float | None:
    """The lowest years-of-experience bar in the text, or None if absent.

    Postings listing several thresholds are nearly always tiered
    alternative qualifications ("Bachelor's AND 2+ years, OR Master's AND
    10+ years") where the lowest number is the real entry bar. Taking the
    minimum occasionally under-penalizes a posting with multiple genuine
    requirements by a few points; taking the maximum buried good jobs
    under tier language, which is the worse error.

    Technology-scoped phrases ("3+ years of experience using AWS") are
    excluded from the minimum when any general phrase exists: they bound
    one skill, not the role, and letting them win hid the real floor
    ("5+ years ... experience" postings scoring as 3-year roles). When
    every phrase is technology-scoped, the lowest still wins — a posting
    whose only stated bar is "5+ years of experience with React" means
    five years.

    Postings that never say "experience" fall back to
    _YEARS_OF_ENGINEERING_PATTERN.

    Whole-number bars come back as int so stored reasons read "5 years",
    not "5.0 years"; fractional bars ("1.5+ years") stay float.
    """
    years: list[float] = []
    general: list[float] = []
    for m in _YEARS_PATTERN.finditer(text):
        value = float(m.group(1))
        years.append(value)
        if not _TECH_SCOPED_PATTERN.match(text[m.end():]):
            general.append(value)
    if not years:
        # Fallback for postings that never use the word "experience".
        years = _years_of_engineering(text)
    if not years:
        return None
    lowest = min(general) if general else min(years)
    return int(lowest) if lowest.is_integer() else lowest
