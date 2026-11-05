"""Pure outreach logic: company matching, follow-ups, status moves, summaries.

No I/O. Contacts and interactions are any mapping (dict or sqlite3.Row),
dates are ISO strings on the way in and datetime.date internally, and cfg is
the outreach section of profile.yaml. Rules that stop or flag something
return a human-readable reason.
"""

import re
from collections import Counter
from datetime import date, timedelta
from typing import Mapping

import db

# The order a contact moves through before anyone replies.
_PRE_REPLY = ("Not started", "Drafted", "Sent", "Follow-up 1 sent", "Follow-up 2 sent")
_NEXT_OUTBOUND = {
    "Not started": "Sent",
    "Drafted": "Sent",
    "Sent": "Follow-up 1 sent",
    "Follow-up 1 sent": "Follow-up 2 sent",
}
# Moving into one of these means the first message has gone out.
_FIRST_CONTACT = ("Sent", "Follow-up 1 sent", "Follow-up 2 sent")
_REPLIED = ("Replied", "Call scheduled", "Referred / submitted")
_CONTACTED = (*_FIRST_CONTACT, *_REPLIED, "Cold")
_INACTIVE = ("Skip", "Cold")


def _to_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _strip_key(name: str, suffixes: list[str]) -> str:
    key = re.sub(r"['’]", "", name.lower())
    key = re.sub(r"[^\w\s&]", " ", key)
    key = " ".join(key.split())
    # Longest first, so "financial group" wins over "group".
    for suffix in sorted(suffixes, key=len, reverse=True):
        suffix = suffix.lower()
        if key.endswith(f" {suffix}"):
            return key[: -len(suffix) - 1].strip()
    return key


def company_key(name: str | None, cfg: Mapping) -> str:
    """Comparable form of a company name, '' for a blank one.

    Each company_aliases entry is a list whose first item is the canonical
    name. Aliases are checked before and after suffix stripping, so an alias
    can be written the way a posting spells it.
    """
    if not name or not name.strip():
        return ""
    suffixes = cfg.get("company_suffixes") or []
    key = _strip_key(name, suffixes)
    for group in cfg.get("company_aliases") or []:
        keys = {_strip_key(alias, suffixes) for alias in group}
        if key in keys:
            return _strip_key(group[0], suffixes)
    return key


def same_company(a: str | None, b: str | None, cfg: Mapping) -> bool:
    key = company_key(a, cfg)
    return bool(key) and key == company_key(b, cfg)


def add_business_days(start: str | date, days: int) -> date:
    """Advance by weekdays only, matching a spreadsheet's WORKDAY()."""
    current = _to_date(start)
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def follow_up_due(contact: Mapping, cfg: Mapping) -> date | None:
    """When the next follow-up is due, or None if none is.

    Both follow-ups count from the first message, not the previous follow-up.
    """
    if not contact["date_contacted"]:
        return None
    rules = cfg["follow_up"]
    days = {
        "Sent": rules["first_business_days"],
        "Follow-up 1 sent": rules["second_business_days"],
    }.get(contact["status"])
    if days is None:
        return None
    return add_business_days(contact["date_contacted"], days)


def attention_reason(contact: Mapping, as_of: str | date, cfg: Mapping) -> str | None:
    """Why this contact needs action on as_of, or None."""
    due = follow_up_due(contact, cfg)
    if due is not None and due <= _to_date(as_of):
        label = "First" if contact["status"] == "Sent" else "Second"
        return f"{label} follow-up due {due.isoformat()}"
    if contact["status"] == "Follow-up 2 sent":
        return "No reply after two follow-ups: mark Cold"
    if contact["status"] == "Replied" and not (contact["next_step"] or "").strip():
        return "Replied with no next step: respond within 24 hours"
    return None


def follow_ups_due(
    contacts: list[Mapping], as_of: str | date, cfg: Mapping
) -> list[tuple[Mapping, str]]:
    """(contact, reason) for everything needing action, oldest due date first.

    Contacts with no due date (replies and Cold prompts) sort first, because
    replies get a 24-hour response.
    """
    items = []
    for contact in contacts:
        reason = attention_reason(contact, as_of, cfg)
        if reason:
            due = follow_up_due(contact, cfg)
            items.append((due or date.min, contact["id"], contact, reason))
    items.sort(key=lambda item: item[:2])
    return [(contact, reason) for _, _, contact, reason in items]


def block_reason(
    company: str | None, email: str | None, cfg: Mapping
) -> str | None:
    """Why this person must never be contacted, or None.

    Matches do_not_contact by company name or email domain (subdomains
    included), so a contact filed under another company is still caught.
    """
    domain = email.rsplit("@", 1)[-1].lower() if email and "@" in email else ""
    for rule in cfg.get("do_not_contact") or []:
        domains = [d.lower() for d in rule.get("email_domains") or []]
        hit_domain = any(domain == d or domain.endswith(f".{d}") for d in domains)
        if hit_domain or same_company(company, rule["company"], cfg):
            return f"Do not contact: {rule['reason']}"
    return None


def double_up_warning(company: str | None, cfg: Mapping) -> str | None:
    """Warning for cold outreach to a company already being handled."""
    for rule in cfg.get("dont_double_up") or []:
        if same_company(company, rule["company"], cfg):
            return f"{rule['company']} is already covered by {rule['covered_by']}"
    return None


def status_change(
    contact: Mapping, new_status: str, today: str | date, cfg: Mapping
) -> dict:
    """Fields to write for a status change. Raises ValueError if not allowed.

    Marking a contact sent stamps date_contacted, which starts the follow-up
    clock. Blocked contacts can only ever be Skip.
    """
    if new_status not in db.CONTACT_STATUSES:
        raise ValueError(f"unknown status: {new_status}")
    reason = block_reason(contact["company"], contact["email"], cfg)
    if reason and new_status != "Skip":
        raise ValueError(reason)
    fields = {"status": new_status}
    if new_status in _FIRST_CONTACT and not contact["date_contacted"]:
        fields["date_contacted"] = _to_date(today).isoformat()
    return fields


def apply_interaction(
    contact: Mapping, direction: str, on_date: str | date, cfg: Mapping
) -> dict:
    """Fields to write after logging an interaction. May be empty.

    Inbound: records last_reply and moves pre-reply or Cold contacts to
    Replied. Skip never changes, and neither does anything past Replied.
    Outbound: moves the contact one step along the send sequence, and raises
    ValueError for a blocked contact. An inbound message from a blocked
    contact can still be logged, since they reached out first.
    """
    day = _to_date(on_date).isoformat()
    status = contact["status"]
    if direction == "in":
        fields = {}
        if not contact["last_reply"] or contact["last_reply"] < day:
            fields["last_reply"] = day
        if status in _PRE_REPLY or status == "Cold":
            fields["status"] = "Replied"
        return fields
    if direction != "out":
        raise ValueError(f"unknown direction: {direction}")
    reason = block_reason(contact["company"], contact["email"], cfg)
    if reason:
        raise ValueError(reason)
    next_status = _NEXT_OUTBOUND.get(status)
    if next_status is None:
        return {}
    return status_change(contact, next_status, day, cfg)


def has_referral_path(contact: Mapping) -> bool:
    """True for a real connection: your own pipeline, or someone who replied.

    A name on a reference list is not a path, however promising it looks.
    """
    if contact["status"] in _INACTIVE:
        return False
    return contact["source"] == "own_pipeline" or bool(contact["last_reply"])


def reply_rate(contacts: list[Mapping]) -> float:
    """Replied or further, out of everyone contacted. 0.0 if nobody yet."""
    contacted = sum(1 for c in contacts if c["status"] in _CONTACTED)
    if not contacted:
        return 0.0
    return sum(1 for c in contacts if c["status"] in _REPLIED) / contacted


def _week_bounds(week_of: str | date) -> tuple[date, date]:
    day = _to_date(week_of)
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def weekly_summary(
    contacts: list[Mapping],
    interactions: list[Mapping],
    week_of: str | date,
    cfg: Mapping,
) -> dict:
    """Pipeline counts plus this week's progress against the weekly rhythm.

    Follow-ups due are measured as of week_of. Targets are shown as progress
    and never enforced.
    """
    monday, sunday = _week_bounds(week_of)
    tracks = {c["id"]: c["track"] for c in contacts}

    def in_week(value: str | None) -> bool:
        return bool(value) and monday <= _to_date(value) <= sunday

    def first_touches(track: str) -> int:
        return sum(
            1 for c in contacts
            if c["track"] == track and in_week(c["date_contacted"])
        )

    by_track: dict[str, Counter] = {}
    for contact in contacts:
        by_track.setdefault(contact["track"], Counter())[contact["status"]] += 1
    targets = cfg["weekly_targets"]
    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "by_track": {track: dict(counts) for track, counts in by_track.items()},
        "by_status": dict(Counter(c["status"] for c in contacts)),
        "follow_ups_due": len(follow_ups_due(contacts, week_of, cfg)),
        "reply_rate": reply_rate(contacts),
        "progress": {
            "agency_first_touches": {
                "done": first_touches("agency"),
                "target": targets["agency_first_touches"],
            },
            "company_recruiter_emails": {
                "done": first_touches("company_recruiter"),
                "target_min": targets["company_recruiter_emails_min"],
                "target_max": targets["company_recruiter_emails_max"],
            },
            "referral_touches": {
                "done": sum(
                    1 for i in interactions
                    if i["direction"] == "out"
                    and in_week(i["date"])
                    and tracks.get(i["contact_id"]) == "referral"
                ),
                "target": targets["referral_touches"],
            },
        },
    }
