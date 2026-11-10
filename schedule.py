"""Daily schedule: the plan from profile.yaml schedule: against what happened.

Pure functions like gates.py and score.py. The plan is a list of time blocks
per weekday (with per-day overrides and a themed block whose focus rotates);
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


def day_plan(cfg: Mapping, day: str | date) -> list[dict]:
    """The blocks for one day, overrides and theme applied. Empty on a rest day."""
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
              logs: list[Mapping], now: datetime) -> dict:
    """One day's blocks with status, evidence and adherence."""
    day = _to_date(day)
    today = now.date()
    iso = day.isoformat()
    by_key = {row["block_key"]: row for row in logs if row["day"] == iso}
    blocks = []
    for block in day_plan(cfg, day):
        count, lines = _evidence_for(block, iso, activity)
        log = by_key.get(block["key"])
        status = _status(block, day, today, now, count, log is not None)
        auto = block["kind"] != "manual"
        blocks.append({
            **block,
            "start": ampm(block["start"]),
            "end": ampm(block["end"]),
            "status": status,
            "count": count if auto else (1 if log else 0),
            "evidence": lines,
            "logged": log is not None,
            "note": log["note"] if log else None,
            "logged_by": log["source"] if log else None,
        })
    return {
        "day": iso,
        "weekday": weekday_key(day),
        "workday": bool(blocks) or is_workday(cfg, day),
        "is_today": day == today,
        "blocks": blocks,
        "adherence": adherence(blocks),
    }


def adherence(blocks: list[Mapping]) -> dict:
    """done = 1, partial = ½, over every block of the day that has started."""
    counted = [b for b in blocks if b["status"] not in ("planned", "upcoming", "now")]
    done = sum(1 for b in counted if b["status"] == "done")
    partial = sum(1 for b in counted if b["status"] == "partial")
    planned = len(blocks)
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
               logs: list[Mapping], now: datetime) -> dict:
    """The week around day: a per-day adherence strip and weekly targets."""
    monday, sunday = week_bounds(day)
    labels = {b["key"]: b["label"] for b in cfg["blocks"]}
    days = []
    for offset in range(7):
        d = monday + timedelta(days=offset)
        if not is_workday(cfg, d):
            continue
        built = build_day(cfg, d, activity, logs, now)
        days.append({
            "day": built["day"],
            "weekday": built["weekday"],
            "is_today": built["is_today"],
            "future": d > now.date(),
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
