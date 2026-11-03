"""Phase 5 analytics: pure aggregation over application + event rows.

Read-only reporting — no writes, no LLM calls. Progress is derived from
the events history rather than the current lane, so an application that
is now rejected still counts every stage it ever reached. Reaching a
stage implies every earlier one: a card moved straight from considering
to interviewing still counts as applied. rejected is terminal, not
progress, and events from the retired screening/ghosted lanes rank as
no progress via _RANK.get.

Tunables (score band edges, fast-apply threshold) come from the
analytics section of config/profile.yaml via the caller.
"""

from datetime import date

from scoring.text import contains_keyword

STAGES = ("considering", "ready", "applied", "interviewing", "offer")
_RANK = {name: rank for rank, name in enumerate(STAGES)}
APPLIED = _RANK["applied"]
ADVANCED = _RANK["interviewing"]  # past Applied = the employer replied
TERMINAL = ("rejected",)  # skipped stays out: a Stage 2 skip was never in play


def _rate(n: int, d: int) -> int | None:
    """Whole-number percentage, or None when there is no denominator."""
    return None if d == 0 else round(100 * n / d)


def compute_reached(apps: list[dict], lane_events: list[dict]) -> dict[int, int]:
    """Furthest stage rank each application ever reached.

    Sources: the current lane, every lane_change destination, and a set
    applied_at (a manually back-filled date counts as having applied even
    if the card never sat in the applied lane).
    """
    reached: dict[int, int] = {}
    for app in apps:
        rank = _RANK.get(app["lane"], -1)  # terminal lanes carry no progress
        if app["applied_at"]:
            rank = max(rank, APPLIED)
        reached[app["id"]] = max(rank, 0)  # every app at least considered
    for event in lane_events:
        app_id = event["application_id"]
        if app_id not in reached:
            continue
        destination = event["detail"].rsplit(" -> ", 1)[-1]
        rank = _RANK.get(destination, -1)
        if rank > reached[app_id]:
            reached[app_id] = rank
    return reached


def funnel(apps: list[dict], reached: dict[int, int], lanes: tuple[str, ...]) -> dict:
    """Current per-lane counts plus ever-reached conversion counts."""
    counts = {lane: 0 for lane in lanes}
    for app in apps:
        counts[app["lane"]] += 1
    return {
        "lanes": [{"name": lane, "count": counts[lane]} for lane in lanes],
        "reached": [
            {
                "stage": stage,
                "count": sum(1 for a in apps if reached[a["id"]] >= _RANK[stage]),
            }
            for stage in STAGES[1:]
        ],
    }


def _applied_apps(apps: list[dict], reached: dict[int, int]) -> list[dict]:
    return [a for a in apps if reached[a["id"]] >= APPLIED]


def _advanced(apps: list[dict], reached: dict[int, int]) -> int:
    return sum(1 for a in apps if reached[a["id"]] >= ADVANCED)


def _row(label: str, group: list[dict], reached: dict[int, int]) -> dict:
    advanced = _advanced(group, reached)
    return {
        "label": label,
        "applied": len(group),
        "advanced": advanced,
        "rate": _rate(advanced, len(group)),
    }


def reply_rate_by_band(
    apps: list[dict], reached: dict[int, int], edges: list[int]
) -> list[dict]:
    """Reply rate (advanced past Applied) for applied jobs, by score band."""
    applied = _applied_apps(apps, reached)
    edges = sorted(edges)
    bounds = [None, *edges, None]
    rows = []
    for lo, hi in zip(bounds, bounds[1:]):
        label = f"< {hi}" if lo is None else (f"{lo}+" if hi is None else f"{lo}–{hi}")
        group = [
            a
            for a in applied
            if a["score"] is not None
            and (lo is None or a["score"] >= lo)
            and (hi is None or a["score"] < hi)
        ]
        rows.append(_row(label, group, reached))
    unscored = [a for a in applied if a["score"] is None]
    if unscored:
        rows.append(_row("no score", unscored, reached))
    return rows


def outcomes_by_resume(apps: list[dict], reached: dict[int, int]) -> list[dict]:
    """Applied jobs grouped by resume_version: advanced / offers / dead."""
    groups: dict[str, list[dict]] = {}
    for app in _applied_apps(apps, reached):
        groups.setdefault(app["resume_version"] or "(not set)", []).append(app)
    rows = []
    for version, group in groups.items():
        rows.append(
            _row(version, group, reached)
            | {
                "offers": sum(1 for a in group if reached[a["id"]] >= _RANK["offer"]),
                "dead": sum(1 for a in group if a["lane"] in TERMINAL),
            }
        )
    return sorted(rows, key=lambda r: (-r["applied"], r["label"]))


def speed_effect(
    apps: list[dict], reached: dict[int, int], fast_hours: int
) -> dict:
    """Did applying within fast_hours of first_seen advance more often?

    applied_at is a local date and first_seen a UTC datetime; comparing
    calendar dates is fine at this granularity.
    """
    fast: list[dict] = []
    slow: list[dict] = []
    unknown = 0
    for app in _applied_apps(apps, reached):
        try:
            applied_on = date.fromisoformat(str(app["applied_at"]))
            seen_on = date.fromisoformat(str(app["first_seen"])[:10])
        except (TypeError, ValueError):
            unknown += 1
            continue
        days = (applied_on - seen_on).days
        (fast if days * 24 <= fast_hours else slow).append(app)
    return {
        "rows": [
            _row(f"within {fast_hours}h", fast, reached),
            _row("later", slow, reached),
        ],
        "unknown": unknown,
    }


def keywords_that_advance(
    apps: list[dict], reached: dict[int, int], keywords: list[str]
) -> dict:
    """Profile keyword hits in descriptions of advanced vs stalled applied jobs.

    Only keywords that hit at least one applied job are listed; a missing
    description simply never matches.
    """
    applied = _applied_apps(apps, reached)
    advanced = [a for a in applied if reached[a["id"]] >= ADVANCED]
    stalled = [a for a in applied if reached[a["id"]] < ADVANCED]
    rows = []
    for keyword in keywords:
        hits_advanced = sum(
            1 for a in advanced if contains_keyword(a["description"] or "", keyword)
        )
        hits_stalled = sum(
            1 for a in stalled if contains_keyword(a["description"] or "", keyword)
        )
        if hits_advanced or hits_stalled:
            rows.append(
                {"keyword": keyword, "advanced": hits_advanced, "stalled": hits_stalled}
            )
    return {"rows": rows, "advanced_total": len(advanced), "stalled_total": len(stalled)}


def build(
    apps: list[dict],
    lane_events: list[dict],
    *,
    lanes: tuple[str, ...],
    keywords: list[str],
    band_edges: list[int],
    fast_hours: int,
) -> dict:
    """Everything the /analytics page renders, in one pass."""
    reached = compute_reached(apps, lane_events)
    applied_n = len(_applied_apps(apps, reached))
    advanced_n = _advanced(apps, reached)
    return {
        "summary": {
            "tracked": len(apps),
            "applied": applied_n,
            "advanced": advanced_n,
            "reply_rate": _rate(advanced_n, applied_n),
            "offers": sum(1 for a in apps if reached[a["id"]] >= _RANK["offer"]),
        },
        "funnel": funnel(apps, reached, lanes),
        "bands": reply_rate_by_band(apps, reached, band_edges),
        "resumes": outcomes_by_resume(apps, reached),
        "speed": speed_effect(apps, reached, fast_hours),
        "keywords": keywords_that_advance(apps, reached, keywords),
        "total_apps": len(apps),
    }
