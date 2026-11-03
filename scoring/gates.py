"""Stage 1 hard gates: deterministic disqualifiers, drop-don't-score.

Pure functions: input is a job dict plus the `gates:` section of
profile.yaml, output is a GateResult. Every failure carries a
human-readable reason prefixed with its gate name ("location: ...") so
callers can both display and aggregate rejections. All failing gates are
reported, not just the first.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from scoring.text import contains_keyword, matching_keywords, required_experience_years

UTC_FORMAT = "%Y-%m-%d %H:%M:%S"  # matches SQLite datetime('now')

# Posting work type is inferred from explicit markers in the location
# string (checked in this order; "NYC or Remote" reads as remote, the
# most permissive claim). Conflicts are asymmetric on purpose: a hybrid
# candidate can work a remote or an onsite posting (geography is the
# location gate's job), a remote-only candidate cannot work postings
# that require office presence, an onsite candidate cannot work
# remote-only postings.
_WORK_TYPE_MARKERS = [
    ("remote", ["remote", "distributed"]),
    ("hybrid", ["hybrid"]),
    ("onsite", ["onsite", "on-site", "in office", "in-office", "office-based"]),
]
_WORK_TYPE_CONFLICTS = {
    "remote": {"onsite", "hybrid"},
    "hybrid": set(),
    "onsite": {"remote"},
}


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def detect_work_type(location: str | None) -> str | None:
    """'remote' | 'hybrid' | 'onsite' from location markers, else None."""
    if not location:
        return None
    for work_type, markers in _WORK_TYPE_MARKERS:
        if any(contains_keyword(location, marker) for marker in markers):
            return work_type
    return None


def _excluded_title_reason(title: str, excluded: list[str]) -> str | None:
    hits = matching_keywords(title, excluded)
    if not hits:
        return None
    listed = ", ".join(f"'{kw}'" for kw in hits)
    return f"excluded-title: title contains {listed}"


def _location_reason(location: str | None, allowed: list[str]) -> str | None:
    if not allowed:
        return None
    if not location:
        return "location: posting has no location to check against allowed_locations"
    if any(part.lower() in location.lower() for part in allowed):
        return None
    return f"location: '{location}' matches none of allowed_locations"


def _work_type_reason(location: str | None, work_cfg: dict) -> str | None:
    if not work_cfg.get("reject_conflicts"):
        return None
    posting = detect_work_type(location)
    mine = work_cfg["mine"]
    if posting in _WORK_TYPE_CONFLICTS.get(mine, set()):
        return f"work-type: posting is {posting}-marked, profile work type is {mine}"
    return None


def _clearance_reason(text: str, clearance_keywords: list[str]) -> str | None:
    hits = matching_keywords(text, clearance_keywords)
    if not hits:
        return None
    listed = ", ".join(f"'{kw}'" for kw in hits)
    return f"clearance: posting mentions {listed}"


def _experience_reason(description: str, max_years: int | None) -> str | None:
    """Reject postings demanding more experience than max_years. Absent or
    zero config disables the gate; a description with no years demand passes."""
    if not max_years:
        return None
    required = required_experience_years(description)
    if required is not None and required > max_years:
        return f"experience: asks for {required} years (max {max_years})"
    return None


def _age_reason(posted_at: str | None, max_days: int | None, now: datetime) -> str | None:
    """Reject postings older than max_days. Absent or zero config disables
    the gate (the delisted gate still catches dead links). Unknown or
    unparseable dates pass — we can't verify what isn't there."""
    if not max_days or not posted_at:
        return None
    try:
        posted = datetime.strptime(posted_at, UTC_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    age_days = (now - posted).days
    if age_days > max_days:
        return f"age: posted {age_days} days ago (max {max_days})"
    return None


def _delisted_reason(
    last_seen: str | None, max_days: int | None, now: datetime
) -> str | None:
    """Reject jobs the sweep hasn't seen on their board for max_days.

    The sweep bumps last_seen on every posting still listed, so a stale
    last_seen means the company took the posting down — the link is dead
    even if the posting is recent. Absent/zero config disables the gate;
    a missing or unparseable last_seen passes (nothing to verify). If a
    posting reappears, the next sweep + rescore un-rejects it.
    """
    if not max_days or not last_seen:
        return None
    try:
        seen = datetime.strptime(last_seen, UTC_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    days = (now - seen).days
    if days > max_days:
        return f"delisted: last seen on the board {days} days ago (max {max_days})"
    return None


def check_gates(job: dict, gates_cfg: dict, *, now: datetime) -> GateResult:
    """Run every gate; a job passes only if no gate objects."""
    title = job.get("title") or ""
    location = job.get("location")
    description = job.get("description") or ""

    reasons = [
        reason
        for reason in (
            _excluded_title_reason(title, gates_cfg["excluded_title_keywords"]),
            _location_reason(location, gates_cfg["allowed_locations"] or []),
            _work_type_reason(location, gates_cfg["work_type"]),
            _clearance_reason(f"{title}\n{description}", gates_cfg["clearance_keywords"]),
            _age_reason(job.get("posted_at"), gates_cfg.get("max_posting_age_days"), now),
            _experience_reason(description, gates_cfg.get("max_required_experience_years")),
            _delisted_reason(
                job.get("last_seen"), gates_cfg.get("delisted_after_days"), now
            ),
        )
        if reason
    ]
    return GateResult(passed=not reasons, reasons=reasons)
