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
from scoring.score import detect_seniority
from scoring.text import contains_keyword

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
# What update_contact may change. track and source are fixed at creation, and
# peer_history is reference material, never edited.
_EDITABLE = (
    "name", "company", "email", "linkedin_url", "kind_detail", "priority",
    "nyc_relevance", "ask", "status", "date_contacted", "last_reply",
    "next_step", "notes", "verify_flag", "gmail_draft_id", "standing_referral",
    "location",
)
_DATE_FIELDS = ("date_contacted", "last_reply")
_PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2, None: 3, "skip": 4}
TRACK_ORDER = ("referral", "mentor", "agency", "company_recruiter", "platform")
BADGE_PATH = "Referral path"
BADGE_CONTACT = "Contact"


def _to_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def iso_date(value: str | date) -> str:
    """Normalize a date. Raises ValueError for anything that isn't one."""
    return _to_date(value).isoformat()


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
    Mentors have no clock: bumping a thread is for cold outreach.
    """
    if not contact["date_contacted"] or contact["track"] == "mentor":
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
    contact: Mapping,
    new_status: str,
    today: str | date,
    cfg: Mapping,
    allow_blocked: bool = False,
) -> dict:
    """Fields to write for a status change. Raises ValueError if not allowed.

    Marking a contact sent stamps date_contacted, which starts the follow-up
    clock. Moving to Replied clears next_step: the old plan no longer applies,
    and an empty next step is what flags the reply for a response. Blocked
    contacts can only ever be Skip.
    """
    if new_status not in db.CONTACT_STATUSES:
        raise ValueError(f"unknown status: {new_status}")
    reason = block_reason(contact["company"], contact["email"], cfg)
    if reason and new_status != "Skip" and not allow_blocked:
        raise ValueError(reason)
    fields = {"status": new_status}
    if new_status in _FIRST_CONTACT and not contact["date_contacted"]:
        fields["date_contacted"] = _to_date(today).isoformat()
    if new_status == "Replied" and contact["status"] != "Replied":
        fields["next_step"] = None
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
            fields.update(status_change(contact, "Replied", day, cfg, allow_blocked=True))
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


def check_interaction(channel: str, direction: str, summary: str) -> None:
    """Raises ValueError for anything the interaction log can't hold."""
    if not summary.strip():
        raise ValueError("summary is empty")
    if channel not in db.INTERACTION_CHANNELS:
        raise ValueError(f"channel must be one of {list(db.INTERACTION_CHANNELS)}")
    if direction not in db.INTERACTION_DIRECTIONS:
        raise ValueError(f"direction must be one of {list(db.INTERACTION_DIRECTIONS)}")


def has_referral_path(contact: Mapping) -> bool:
    """True for a real connection: your own pipeline, or someone who replied.

    A name on a reference list is not a path, however promising it looks. A
    mentor isn't one either: they're there for advice, not a way in.
    """
    if contact["status"] in _INACTIVE or contact["track"] == "mentor":
        return False
    return contact["source"] == "own_pipeline" or bool(contact["last_reply"])


def is_watching(contact: Mapping, cfg: Mapping) -> bool:
    """Whether this contact's company should be watched for roles.

    A standing referral has to be a real, active connection, so the flag alone
    never starts a watch on a Skip, Cold or do-not-contact person.
    """
    return (
        bool(contact["standing_referral"])
        and bool(contact["company"])
        and has_referral_path(contact)
        and not block_reason(contact["company"], contact["email"], cfg)
    )


def watch_reason(job: Mapping, cfg: Mapping, years_cfg: Mapping | None) -> str | None:
    """Why a job suits a standing referral, or None if it doesn't.

    Uses the scorer's own level check, so the watcher and the feed always agree
    on what mid-level means. Which levels and titles count is in profile.yaml.
    """
    watch = cfg.get("referral_watch") or {}
    title = job["title"] or ""
    if not any(contains_keyword(title, k) for k in watch.get("title_keywords") or []):
        return None
    level, reason = detect_seniority(title, job["description"], years_cfg)
    return f"{level} level: {reason}" if level in (watch.get("levels") or []) else None


def reply_rate(contacts: list[Mapping]) -> float:
    """Replied or further, out of people you messaged first. 0.0 if nobody yet.

    Measures outreach, so mentors don't count either way, and neither does
    anyone who reached out to you: only a first message of yours stamps
    date_contacted, and an inbound pitch isn't a reply to anything.
    """
    outreach_rows = [
        c for c in contacts if c["track"] != "mentor" and c["date_contacted"]
    ]
    contacted = sum(1 for c in outreach_rows if c["status"] in _CONTACTED)
    if not contacted:
        return 0.0
    return sum(1 for c in outreach_rows if c["status"] in _REPLIED) / contacted


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


def contact_summary(contact: Mapping, as_of: str | date, cfg: Mapping) -> dict:
    """The short form of a contact. Leaves out peer_history and long text."""
    due = follow_up_due(contact, cfg)
    return {
        "contact_id": contact["id"],
        "name": contact["name"],
        "company": contact["company"],
        "location": contact["location"],
        "email": contact["email"],
        "linkedin_url": contact["linkedin_url"],
        "track": contact["track"],
        "priority": contact["priority"],
        "status": contact["status"],
        "date_contacted": contact["date_contacted"],
        "last_reply": contact["last_reply"],
        "next_step": contact["next_step"],
        "follow_up_due": due.isoformat() if due else None,
        "needs_action": attention_reason(contact, as_of, cfg),
        "referral_path": has_referral_path(contact),
        "do_not_contact": block_reason(contact["company"], contact["email"], cfg),
        "verify_flag": contact["verify_flag"],
        "has_gmail_draft": bool(contact["gmail_draft_id"]),
        "standing_referral": bool(contact["standing_referral"]),
    }


def filter_contacts(
    contacts: list[Mapping],
    cfg: Mapping,
    track: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    company: str | None = None,
    search: str | None = None,
    location: str | None = None,
) -> list[Mapping]:
    """Contacts matching every filter, best priority first.

    Skip stays hidden unless status="Skip" asks for it. company matches any
    part of a name, after aliases, so "Chase" and a partial name both work.
    search also looks at names and emails. location ignores case.
    """
    query = company_key(company, cfg)
    place = (location or "").strip().lower()
    text = (search or "").strip().lower()
    text_key = company_key(search, cfg)

    def found(contact: Mapping) -> bool:
        return (
            (text_key and text_key in company_key(contact["company"], cfg))
            or text in (contact["name"] or "").lower()
            or text in (contact["email"] or "").lower()
        )

    rows = [
        c for c in contacts
        if (c["status"] == status if status else c["status"] != "Skip")
        and (not track or c["track"] == track)
        and (not priority or c["priority"] == priority)
        and (not query or query in company_key(c["company"], cfg))
        and (not text or found(c))
        and (not place or (c["location"] or "").lower() == place)
    ]
    return sorted(
        rows,
        key=lambda c: (_PRIORITY_ORDER[c["priority"]], (c["name"] or "").lower(), c["id"]),
    )


def contact_locations(contacts: list[Mapping]) -> list[str]:
    """The location labels in use, for the filter and form suggestions."""
    return sorted({c["location"] for c in contacts if c["location"]})


def visible_contacts(
    contacts: list[Mapping],
    cfg: Mapping,
    track: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    search: str | None = None,
    show_c: bool = False,
    location: str | None = None,
) -> list[Mapping]:
    """What the Contacts page lists. Priority C stays out of the default view.

    Unranked contacts always show, because the people you actually know have no
    priority. Searching or filtering by status, priority or location finds C
    contacts too: someone you look for should never come back empty.
    """
    rows = filter_contacts(
        contacts, cfg, track=track, status=status, priority=priority,
        search=search, location=location,
    )
    if show_c or search or status or priority or location:
        return rows
    return [c for c in rows if c["priority"] != "C"]


def group_by_track(contacts: list[Mapping]) -> list[tuple[str, list[Mapping]]]:
    """(track, contacts) in display order, real relationships first."""
    return [
        (track, rows)
        for track in TRACK_ORDER
        if (rows := [c for c in contacts if c["track"] == track])
    ]


def company_badges(contacts: list[Mapping], cfg: Mapping) -> dict[str, str]:
    """Job-card labels by company_key.

    "Referral path" for a real connection, "Contact" for an active A or B
    contact with no connection yet. A real connection always wins.
    """
    badges: dict[str, str] = {}
    for contact in contacts:
        key = company_key(contact["company"], cfg)
        if not key or contact["status"] in _INACTIVE:
            continue
        if has_referral_path(contact):
            badges[key] = BADGE_PATH
        elif contact["priority"] in ("A", "B"):
            badges.setdefault(key, BADGE_CONTACT)
    return badges


def linked_badges(
    contacts: list[Mapping], links: list[Mapping]
) -> dict[int, str]:
    """Referral labels by application_id, for contacts tied to a specific job.

    An agency recruiter's company won't match the job's, so the link is what
    connects them.
    """
    by_id = {c["id"]: c for c in contacts}
    return {
        link["application_id"]: BADGE_PATH
        for link in links
        if link["contact_id"] in by_id and has_referral_path(by_id[link["contact_id"]])
    }


def contacts_for_company(
    contacts: list[Mapping],
    company: str | None,
    linked_ids: set[int],
    as_of: str | date,
    cfg: Mapping,
) -> dict:
    """Active contacts for a company, grouped by track, with a referral flag.

    Includes anyone explicitly linked to the application, such as an agency
    recruiter who works at a different company. Skip and Cold are left out.
    """
    tracks: dict[str, list[dict]] = {}
    for contact in contacts:
        if contact["status"] in _INACTIVE:
            continue
        if contact["id"] in linked_ids or same_company(contact["company"], company, cfg):
            summary = contact_summary(contact, as_of, cfg)
            tracks.setdefault(contact["track"], []).append(summary)
    paths = [
        summary["name"] or summary["company"]
        for rows in tracks.values()
        for summary in rows
        if summary["referral_path"]
    ]
    return {
        "company": company,
        "referral_path": bool(paths),
        "referral_path_contacts": paths,
        "contacts_by_track": tracks,
        "double_up_warning": double_up_warning(company, cfg),
    }


def suggest_template(contact: Mapping, cfg: Mapping) -> dict | None:
    """The message template that fits this contact, filled in, or None.

    None for anyone who needs a personal message or no message at all: firm-only
    rows, blocked or inactive contacts, people already in a conversation,
    mentors, and referral relationships, apart from an alumni first touch.
    Any {new_detail} placeholder is left for the writer to fill.
    """
    status = contact["status"]
    if (
        not contact["name"]
        or status in _INACTIVE
        or contact["track"] == "mentor"
        or block_reason(contact["company"], contact["email"], cfg)
    ):
        return None
    cold_track = {"agency": "agency", "company_recruiter": "company_recruiter"}.get(
        contact["track"]
    )
    if status in ("Sent", "Follow-up 1 sent"):
        kind = "follow_up" if cold_track else None
    elif status not in ("Not started", "Drafted"):
        kind = None
    elif re.search(r"\balum", contact["kind_detail"] or "", re.IGNORECASE):
        kind = "alumni"
    else:
        kind = cold_track
    if kind is None:
        return None
    template = cfg["templates"][kind]

    def fill(text: str) -> str:
        first = contact["name"].split()[0]
        return text.replace("{first}", first).replace(
            "{company}", contact["company"] or ""
        )

    return {
        "kind": kind,
        "subject": fill(template["subject"]),
        "body": fill(template["body"]),
    }


def _clean_value(field: str, value):
    if isinstance(value, str):
        value = value.strip() or None
    if value is not None and field in _DATE_FIELDS:
        value = _to_date(value).isoformat()
    if field == "standing_referral":
        return 1 if value in (True, 1, "1", "true") else 0
    if value is not None and field == "priority" and value not in db.CONTACT_PRIORITIES:
        raise ValueError(f"unknown priority: {value}")
    return value


def plan_update(
    contact: Mapping, changes: Mapping, today: str | date, cfg: Mapping
) -> tuple[dict, list[str]]:
    """(fields to write, warnings) for an edit. Raises ValueError if not allowed.

    notes always appends, so a skip reason can't be overwritten. Saving a draft
    ID moves a Not started contact to Drafted. Leaving Skip has to go through
    restore_fields, so a contact is never left half restored.
    """
    unknown = set(changes) - set(_EDITABLE)
    if unknown:
        raise ValueError(
            f"can't update {sorted(unknown)}: editable fields are {list(_EDITABLE)}"
        )
    fields = {name: _clean_value(name, value) for name, value in changes.items()}
    merged = {**contact, **fields}
    reason = block_reason(merged["company"], merged["email"], cfg)
    warnings = []

    if reason and contact["status"] != "Skip":
        # A new company or email just revealed this person is off limits.
        fields.update(status="Skip", priority="skip", gmail_draft_id=None)
        warnings.append(f"{reason}. Moved to Skip.")
    elif reason and (
        fields.get("gmail_draft_id")
        or fields.get("status") not in (None, "Skip")
        or fields.get("priority") not in (None, "skip")
    ):
        raise ValueError(reason)

    status = fields.get("status")
    if fields.get("gmail_draft_id") and not status and contact["status"] == "Not started":
        status = "Drafted"
    if status and status != contact["status"]:
        if contact["status"] == "Skip":
            raise ValueError("This contact is on Skip: use restore_contact")
        # Explicit values win, so a reply logged with a new next step keeps it.
        before = {**merged, "status": contact["status"]}
        fields = {**status_change(before, status, today, cfg), **fields}
        if status in ("Drafted", "Sent"):
            warning = double_up_warning(merged["company"], cfg)
            if warning:
                warnings.append(warning)
    if "notes" in fields:
        if fields["notes"] is None:
            del fields["notes"]
        elif contact["notes"]:
            fields["notes"] = f"{contact['notes'].rstrip()}\n\n{fields['notes']}"
    return fields, warnings


def plan_new_contact(
    fields: Mapping, existing: list[Mapping], cfg: Mapping
) -> tuple[dict, list[str]]:
    """(fields to insert, warnings) for a new contact.

    Raises ValueError for bad input. Someone on the do-not-contact list is
    still saved, as Skip with the reason. Callers check for a duplicate email
    first with find_duplicate.
    """
    new = {k: _clean_value(k, v) for k, v in fields.items() if v is not None}
    if not new.get("name") and not new.get("company"):
        raise ValueError("A contact needs a name or a company")
    if new.get("track") not in db.CONTACT_TRACKS:
        raise ValueError(f"track must be one of {list(db.CONTACT_TRACKS)}")
    new.setdefault("source", "research")
    if new["source"] not in db.CONTACT_SOURCES:
        raise ValueError(f"source must be one of {list(db.CONTACT_SOURCES)}")
    warnings = []
    reason = block_reason(new.get("company"), new.get("email"), cfg)
    if reason:
        new.update(status="Skip", priority="skip", notes=reason)
        warnings.append(f"{reason}. Saved as Skip.")
    else:
        warning = double_up_warning(new.get("company"), cfg)
        if warning:
            warnings.append(warning)
    same_name = [
        c for c in existing
        if new.get("name")
        and (c["name"] or "").lower() == new["name"].lower()
        and same_company(c["company"], new.get("company"), cfg)
    ]
    if same_name:
        warnings.append(
            f"Possible duplicate of contact {same_name[0]['id']}: same name and company"
        )
    return new, warnings


def find_duplicate(email: str | None, existing: list[Mapping]) -> Mapping | None:
    """The contact already using this email, ignoring case."""
    if not email or not email.strip():
        return None
    wanted = email.strip().lower()
    return next((c for c in existing if (c["email"] or "").lower() == wanted), None)


def restore_fields(contact: Mapping, cfg: Mapping) -> dict:
    """Fields that bring a skipped contact back. Raises ValueError if blocked.

    The notes are left alone, so the original skip reason stays visible.
    """
    reason = block_reason(contact["company"], contact["email"], cfg)
    if reason:
        raise ValueError(reason)
    if contact["status"] != "Skip":
        raise ValueError("Only skipped contacts can be restored")
    fields = {"status": "Not started"}
    if contact["priority"] == "skip":
        fields["priority"] = "C"
    if not contact["ask"]:
        fields["ask"] = cfg["default_ask"]
    return fields
