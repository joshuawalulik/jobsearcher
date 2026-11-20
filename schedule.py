"""Daily schedule: the plan from profile.yaml schedule: against what happened.

Pure functions like gates.py and score.py. The plan is a list of time blocks
per weekday (with per-day overrides and a themed block whose focus rotates),
and a dated plan replaces it on one calendar day (a weekend of interview
prep, an interview day);
what happened comes from db.fetch_schedule_activity (lane moves, dismissals,
outreach interactions, sweeps) plus day_log rows for blocks marked done by
hand or from a Claude chat. Nothing here reads the database or the clock.

Block status for a day:
    done      evidence met the target, or the block was marked done
    partial   some evidence, short of the target, and the block is over
    now       the clock is inside the block today and it isn't done yet
    upcoming  later today
    missed    over, nothing to show
    planned   a future day
    ongoing   an all_day block (triage) on today or a past day: it lists what
              happened and is never done or missed, and adherence skips it

Adherence counts done as 1 and partial as half, over the blocks planned.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Mapping

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DECIDED_LANES = ("ready", "applied", "skipped")
APPLIED_LANES = ("ready", "applied")
STATUS_ORDER = ("now", "partial", "missed", "upcoming", "done", "planned")

_RANK = {status: i for i, status in enumerate(STATUS_ORDER)}


def _to_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def ampm(value: str) -> str:
    """Display form of an "HH:MM" string: "16:30" -> "4:30 PM"."""
    at = _hhmm(value)
    hour = at.hour % 12 or 12
    return f"{hour}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def weekday_key(day: str | date) -> str:
    return WEEKDAYS[_to_date(day).weekday()]


def is_workday(cfg: Mapping, day: str | date) -> bool:
    return weekday_key(day) in cfg.get("workdays", WEEKDAYS[:5])


def week_bounds(day: str | date) -> tuple[date, date]:
    day = _to_date(day)
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def dated_plan(cfg: Mapping, day: str | date) -> Mapping | None:
    """The dated: entry for this calendar day, if one exists.

    dated: is a list (not a mapping keyed by date) so the public example
    profile never needs real dates as keys. YAML may read an unquoted day
    as a date, so both forms compare by ISO string.
    """
    iso = _to_date(day).isoformat()
    for entry in cfg.get("dated") or ():
        if str(entry.get("day")) == iso:
            return entry
    return None


def _dated_blocks(cfg: Mapping, entry: Mapping) -> list[dict]:
    """A dated plan's blocks. A key that names a base block inherits its
    fields (so `- key: triage` is enough); weekday overrides and themes
    don't apply, because a dated plan says exactly what the day is."""
    base = {b["key"]: b for b in cfg["blocks"]}
    plan = []
    for item in entry.get("blocks") or ():
        block = {**base.get(item["key"], {}), **item}
        block.pop("themed", None)
        if block.get("drop"):
            continue
        block.setdefault("kind", "manual")
        block.setdefault("target", 1)
        plan.append(block)
    return plan


def day_plan(cfg: Mapping, day: str | date) -> list[dict]:
    """The blocks for one day, overrides and theme applied. Empty on a rest day.

    A dated plan for this calendar day replaces the weekday plan, on a rest
    day too (a weekend of prep gets real blocks and a score).
    """
    entry = dated_plan(cfg, day)
    if entry is not None:
        return _dated_blocks(cfg, entry)
    if not is_workday(cfg, day):
        return []
    key = weekday_key(day)
    overrides = (cfg.get("overrides") or {}).get(key) or {}
    themes = cfg.get("themes") or {}
    plan = []
    for base in cfg["blocks"]:
        block = dict(base)
        block.update(overrides.get(block["key"]) or {})
        if block.get("drop"):
            continue
        if block.get("themed") and themes.get(key):
            block["detail"] = themes[key]
        block.setdefault("kind", "manual")
        block.setdefault("target", 1)
        plan.append(block)
    return plan


def block_keys(cfg: Mapping, day: str | date) -> set[str]:
    """Keys that can be logged on this day: every base block plus the day's
    dated blocks."""
    return {b["key"] for b in cfg["blocks"]} | {b["key"] for b in day_plan(cfg, day)}


def block_labels(cfg: Mapping) -> dict[str, str]:
    """Display label per block key: base blocks first, then any key that only
    a dated plan uses."""
    labels = {b["key"]: b["label"] for b in cfg["blocks"]}
    for entry in cfg.get("dated") or ():
        for item in entry.get("blocks") or ():
            if item.get("label"):
                labels.setdefault(item["key"], item["label"])
    return labels


def _lane_to(detail: str) -> str:
    return detail.split("->")[-1].strip()


def _evidence_for(block: Mapping, day: str, activity: Mapping) -> tuple[int, list[str]]:
    """How much of an auto block the records show, with the lines to display."""
    kind = block["kind"]
    if kind == "applications":
        decided: dict[int, str] = {}
        started: dict[int, str] = {}
        for move in activity.get("lane_moves", ()):
            if move["day"] != day:
                continue
            if move["detail"] and "->" in move["detail"]:
                lane = _lane_to(move["detail"])
                if lane in DECIDED_LANES:
                    decided[move["application_id"]] = (
                        f"{move['company']} — {move['title']}: {lane}"
                    )
            else:
                started[move["application_id"]] = f"{move['company']} — {move['title']}: started"
        # A job tracked and decided the same day shows once, as the decision.
        only_started = [line for app_id, line in started.items() if app_id not in decided]
        return len(decided), list(decided.values()) + only_started
    if kind == "outreach":
        lines = []
        count = 0
        for item in activity.get("interactions", ()):
            if item["day"] != day or item["direction"] != "out":
                continue
            count += 1
            who = item["name"] or item["company"] or "a contact"
            what = "new message" if item["first_touch"] else "follow-up"
            lines.append(f"{who}{' at ' + item['company'] if item['name'] and item['company'] else ''}: {what}")
        return count, lines
    if kind == "triage":
        lines = []
        count = 0
        n = activity.get("dismissed", {}).get(day, 0)
        if n:
            count += n
            lines.append(f"{n} job{'s' if n != 1 else ''} dismissed")
        started = [m for m in activity.get("lane_moves", ())
                   if m["day"] == day and not (m["detail"] and "->" in m["detail"])]
        if started:
            count += len(started)
            lines.append(f"{len(started)} job{'s' if len(started) != 1 else ''} tracked")
        inbound = [i for i in activity.get("interactions", ())
                   if i["day"] == day and i["direction"] == "in"]
        if inbound:
            lines.append(f"{len(inbound)} repl{'ies' if len(inbound) != 1 else 'y'} logged")
        sweeps = [ampm(s["at"][:5]) for s in activity.get("sweeps", ()) if s["day"] == day]
        if sweeps:
            lines.append("Sweep ran " + ", ".join(sweeps))
        return count, lines
    return 0, []


def off_day_evidence(cfg: Mapping, day: str, activity: Mapping,
                     logs: list[Mapping]) -> list[str]:
    """What happened on a rest day: the same records the blocks read, in one
    list. Sweeps are left out because launchd ran them, not the user."""
    lines: list[str] = []
    kinds = {
        "applications": {"key": "applications", "kind": "applications", "target": 1},
        "outreach": {"key": "outreach", "kind": "outreach", "target": 1},
        "triage": {"key": "triage", "kind": "triage", "target": 1},
    }
    for block in kinds.values():
        _, found = _evidence_for(block, day, activity)
        lines.extend(line for line in found if not line.startswith("Sweep ran"))
    labels = block_labels(cfg)
    for row in logs:
        if row["day"] != day:
            continue
        label = labels.get(row["block_key"], row["block_key"])
        lines.append(f"{label}: {row['note']}" if row.get("note") else f"{label} marked done")
    return lines


def _status(block: Mapping, day: date, today: date, now: datetime | None,
            count: int, logged: bool) -> str:
    if logged or count >= block["target"]:
        return "done"
    if day > today:
        return "planned"
    if day == today and now is not None:
        clock = now.time()
        if clock < _hhmm(block["start"]):
            return "upcoming"
        if clock < _hhmm(block["end"]):
            return "now"
    return "partial" if count else "missed"


def build_day(cfg: Mapping, day: str | date, activity: Mapping,
              logs: list[Mapping], now: datetime, excused: str | None = None) -> dict:
    """One day's blocks with status, evidence and adherence.

    excused is the reason the whole day is off the clock (travel, sick):
    blocks still show their evidence, a block the records show done stays
    done, every other timed block reads 'excused', and the day gets no score.
    """
    day = _to_date(day)
    excused = (excused or "").strip() or None
    today = now.date()
    iso = day.isoformat()
    by_key = {row["block_key"]: row for row in logs if row["day"] == iso}
    blocks = []
    for block in day_plan(cfg, day):
        count, lines = _evidence_for(block, iso, activity)
        log = by_key.get(block["key"])
        all_day = bool(block.get("all_day"))
        if all_day:
            # Runs through the day (triage): it shows what happened and is
            # never done or missed, so the clock and the log don't apply.
            status = "planned" if day > today else "ongoing"
        else:
            status = _status(block, day, today, now, count, log is not None)
            if excused and status != "done":
                status = "excused"
        auto = block["kind"] != "manual"
        blocks.append({
            **block,
            "all_day": all_day,
            "start": None if all_day else ampm(block["start"]),
            "end": None if all_day else ampm(block["end"]),
            "status": status,
            "count": count if auto else (1 if log else 0),
            "evidence": lines,
            "logged": log is not None,
            "note": log["note"] if log else None,
            "logged_by": log["source"] if log else None,
        })
    workday = bool(blocks) or is_workday(cfg, day)
    adh = adherence(blocks)
    if excused:
        adh = {**adh, "elapsed": 0, "pct": None}
    return {
        "day": iso,
        "weekday": weekday_key(day),
        "workday": workday,
        "is_today": day == today,
        "excused": excused,
        # A dated plan's one-line title ("JPM prep: Kafka and front end").
        "focus": (dated_plan(cfg, day) or {}).get("title"),
        "blocks": blocks,
        "adherence": adh,
        # A rest day still shows what you did, without a score.
        "off_day_evidence": [] if workday else off_day_evidence(cfg, iso, activity, logs),
    }


def adherence(blocks: list[Mapping]) -> dict:
    """done = 1, partial = ½, over every timed block of the day that has started.

    All-day blocks (status ongoing) are neither planned nor counted.
    """
    timed = [b for b in blocks if b["status"] != "ongoing" and not b.get("all_day")]
    counted = [b for b in timed if b["status"] not in ("planned", "upcoming", "now", "excused")]
    done = sum(1 for b in counted if b["status"] == "done")
    partial = sum(1 for b in counted if b["status"] == "partial")
    planned = len(timed)
    scored = done + 0.5 * partial
    return {
        "done": done,
        "partial": partial,
        "elapsed": len(counted),
        "planned": planned,
        "pct": round(100 * scored / len(counted)) if counted else None,
    }


def _week_counts(cfg: Mapping, monday: date, sunday: date, activity: Mapping,
                 logs: list[Mapping]) -> dict[str, int]:
    start, end = monday.isoformat(), sunday.isoformat()
    applied: set[int] = set()
    for move in activity.get("lane_moves", ()):
        if start <= move["day"] <= end and move["detail"] and "->" in move["detail"]:
            if _lane_to(move["detail"]) in APPLIED_LANES:
                applied.add(move["application_id"])
    outreach_new = 0
    replied: set[int] = set()
    for item in activity.get("interactions", ()):
        if not start <= item["day"] <= end:
            continue
        if item["direction"] == "out" and item["first_touch"]:
            outreach_new += 1
        elif item["direction"] == "in":
            replied.add(item["contact_id"])
    counts = {
        "applications": len(applied),
        "outreach_new": outreach_new,
        "conversations": len(replied),
    }
    for row in logs:
        if start <= row["day"] <= end:
            counts[row["block_key"]] = counts.get(row["block_key"], 0) + 1
    return counts


_TARGET_LABELS = {
    "applications": "Applications",
    "outreach_new": "New outreach",
    "conversations": "Conversations",
}


def build_week(cfg: Mapping, day: str | date, activity: Mapping,
               logs: list[Mapping], now: datetime,
               excuses: Mapping[str, str] | None = None) -> dict:
    """The week around day: a per-day adherence strip and weekly targets.

    excuses maps ISO day -> reason for excused days, which show in the strip
    but carry no score, so the week average skips them.
    """
    monday, sunday = week_bounds(day)
    excuses = excuses or {}
    labels = block_labels(cfg)
    days = []
    for offset in range(7):
        d = monday + timedelta(days=offset)
        built = build_day(cfg, d, activity, logs, now, excuses.get(d.isoformat()))
        off_day = not built["workday"]
        if off_day and not built["off_day_evidence"]:
            continue  # a quiet weekend day stays out of the strip
        days.append({
            "day": built["day"],
            "weekday": built["weekday"],
            "is_today": built["is_today"],
            "future": d > now.date(),
            "off_day": off_day,
            "excused": built["excused"],
            "item_count": len(built["off_day_evidence"]),
            "pct": built["adherence"]["pct"],
            "done": built["adherence"]["done"],
            "planned": built["adherence"]["planned"],
        })
    counts = _week_counts(cfg, monday, sunday, activity, logs)
    targets = []
    for key, target in (cfg.get("weekly_targets") or {}).items():
        done = counts.get(key, 0)
        targets.append({
            "key": key,
            "label": _TARGET_LABELS.get(key) or labels.get(key) or key.replace("_", " ").capitalize(),
            "done": done,
            "target": target,
            "pct": min(100, round(100 * done / target)) if target else 0,
        })
    scored = [d["pct"] for d in days if d["pct"] is not None]
    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "days": days,
        "targets": targets,
        "pct": round(sum(scored) / len(scored)) if scored else None,
    }


def next_block(blocks: list[Mapping]) -> dict | None:
    """What to do now: the block in progress, else the next one not done."""
    for status in ("now", "upcoming"):
        for block in blocks:
            if block["status"] == status:
                return block
    return None
