"""MCP server exposing the tracker and outreach contacts to Claude Desktop.

Stdio server registered in Claude Desktop's claude_desktop_config.json
(command: .venv/bin/python, args: [<abs path to this file>]). Reads
data/jobs.db directly, so it works whether or not uvicorn is running.
For applications, the one write tool appends to notes; lane moves stay
in the web UI. Contact tools can read and edit contacts, with the rules
living in outreach.py. Nothing here sends email: drafts are created
through the Gmail connector and recorded with update_contact.

Run by hand for debugging: .venv/bin/python mcp_server.py
"""

import functools
import json
from datetime import date, datetime

import yaml
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import db
import outreach
import referral_watch
import schedule
from profile_io import PROFILE_PATH

mcp = MCPServer(
    "jobsearcher",
    instructions=(
        "Job-search tracker for Joshua's local JobSearcher app. "
        "list_considering_jobs shows what's queued for an application "
        "workflow pass (has_notes=false usually means not yet processed); "
        "get_job returns the full description and score breakdown; "
        "save_application_note appends workflow output to the tracker card. "
        "When asked to run the application workflow for new/considering "
        "jobs without naming a specific one: call list_considering_jobs, "
        "show the unprocessed jobs as a short numbered list (company, "
        "title, score), and ask which one to work on. Process only the "
        "chosen job (get_job, then the workflow), and offer the next "
        "unprocessed job when it's done. Never batch-process all jobs "
        "unless explicitly asked to do them all.\n\n"
        "Outreach contacts: list_contacts and get_contact read recruiter and "
        "referral contacts; list_followups_due and weekly_summary show what "
        "needs doing; contacts_for_job shows who is at a job's company. "
        "Rules: nothing here sends email. Create Gmail drafts through the "
        "Gmail connector only, record the draft with update_contact, and let "
        "the user send it and then mark it Sent. Never invent a name or an "
        "email. peer_history is someone else's experience with that contact, "
        "not the user's relationship, so never imply prior contact from it. "
        "Only referral_path=true means a real connection; a name on a list is "
        "not a way in. Company recruiters from a list get a pointer ask (who "
        "handles experienced engineering hiring), never a job request. "
        "Agencies get asked whether they are working relevant searches. "
        "Referrals need rapport first, so a first touch is never a referral "
        "ask. list_referral_watch shows open roles to send to standing "
        "referrals; set standing_referral with update_contact when someone "
        "offers a referral for any future role. Mentors are there for advice and information only: never ask "
        "them for a job, a referral or an intro unless the user says to. "
        "Show any warnings a tool returns to the user. If do_not_contact "
        "is set, never draft to or suggest that person.\n\n"
        "Daily schedule: today_plan shows the day's time blocks with status "
        "and this week's targets; use it when the user asks what to do now "
        "or how the day is going. Applications and outreach blocks fill in "
        "from the tracker; prep blocks (coding, prep) are done only "
        "when logged, so call log_block with a short note after running a "
        "prep session in chat or when the user says a block is done. Never "
        "log a block the user didn't do."
    ),
)


def _connect():
    # db.DB_PATH passed explicitly so tests can monkeypatch it.
    return db.get_connection(db.DB_PATH, check_same_thread=False)


@mcp.tool()
def list_considering_jobs() -> list[dict]:
    """Jobs in the tracker's 'considering' lane, most recently touched first.

    has_notes=false usually means the application workflow hasn't been
    run for that job yet. Use get_job(app_id) for the full description.
    """
    conn = _connect()
    try:
        rows = [r for r in db.fetch_applications(conn) if r["lane"] == "considering"]
        return [
            {
                "app_id": r["id"],
                "company": r["company"],
                "title": r["title"],
                "location": r["location"],
                "score": r["score"],
                "url": r["url"],
                "has_notes": bool((r["notes"] or "").strip()),
                "tracked_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


@mcp.tool()
def get_job(app_id: int) -> dict:
    """Full detail for one tracked job by app_id (from list_considering_jobs).

    Includes the complete job description, the deterministic score
    breakdown (matched/missing keywords, coverage), and current notes.
    """
    conn = _connect()
    try:
        row = db.get_application(conn, app_id)
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"no application with app_id {app_id}")
    try:
        breakdown = json.loads(row["score_breakdown"]) if row["score_breakdown"] else None
    except json.JSONDecodeError:
        breakdown = row["score_breakdown"]
    return {
        "app_id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "url": row["url"],
        "lane": row["lane"],
        "score": row["score"],
        "score_breakdown": breakdown,
        "description": row["description"],
        "notes": row["notes"],
        "resume_version": row["resume_version"],
        "tracked_at": row["created_at"],
    }


@mcp.tool()
def save_application_note(app_id: int, text: str) -> str:
    """Append text to an application's notes on the tracker card.

    Appends (never overwrites), separated by a blank line. Use it to
    save application-workflow output — tailored bullets, a cover letter
    draft, a fit summary — so it shows up in the tracker UI.
    """
    if not text.strip():
        raise ValueError("text is empty")
    conn = _connect()
    try:
        if not db.append_application_note(conn, app_id, text.strip()):
            raise ValueError(f"no application with app_id {app_id}")
    finally:
        conn.close()
    return f"Saved to notes for app_id {app_id}."


class Refused(ToolError, ValueError):
    """A rule said no. The MCP library hides a plain ValueError's message from
    the client, and the reason is the whole point of a refusal, so it has to
    arrive as a ToolError.
    """


def _contact_tool(fn):
    """Register a contact tool whose ValueErrors reach the client as reasons."""

    @functools.wraps(fn)
    def explained(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise Refused(str(exc)) from exc

    return mcp.tool()(explained)


def _outreach_cfg() -> dict:
    # Read per call so profile edits apply without restarting the server.
    return yaml.safe_load(PROFILE_PATH.read_text())["outreach"]


def _years_cfg() -> dict:
    profile = yaml.safe_load(PROFILE_PATH.read_text())
    return profile["scoring"]["seniority"]["years_to_level"]


def _today() -> str:
    return date.today().isoformat()


def _require_contact(conn, contact_id: int):
    row = db.get_contact(conn, contact_id)
    if row is None:
        raise ValueError(f"no contact with contact_id {contact_id}")
    return row


def _write_and_read(conn, contact_id: int, fields: dict, warnings: list[str], cfg: dict) -> dict:
    db.update_contact(conn, contact_id, **fields)
    row = db.get_contact(conn, contact_id)
    return {"contact": outreach.contact_summary(row, _today(), cfg), "warnings": warnings}


@_contact_tool
def list_contacts(
    track: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    company: str | None = None,
    location: str | None = None,
) -> list[dict]:
    """Recruiter and referral contacts, best priority first.

    Filters: track (agency, company_recruiter, referral, mentor, platform), status,
    priority (A, B, C, skip), company (any part of the name) and location
    (the market they cover, such as NY, AZ or Remote). Skipped
    contacts are hidden unless status="Skip". Use get_contact(contact_id) for
    peer history, the interaction log and a message template.
    """
    cfg = _outreach_cfg()
    conn = _connect()
    try:
        rows = db.fetch_contacts(conn)
    finally:
        conn.close()
    matches = outreach.filter_contacts(
        rows, cfg, track=track, status=status, priority=priority, company=company,
        location=location,
    )
    return [outreach.contact_summary(c, _today(), cfg) for c in matches]


@_contact_tool
def get_contact(contact_id: int) -> dict:
    """Everything about one contact, plus a message template if one fits.

    peer_history is someone else's experience with this contact, kept as a
    reference for what might happen. It is not the user's relationship.
    suggested_template is null when a personal message is needed or none
    should be sent.
    """
    cfg = _outreach_cfg()
    conn = _connect()
    try:
        row = _require_contact(conn, contact_id)
        interactions = db.fetch_interactions(conn, contact_id)
    finally:
        conn.close()
    return {
        **dict(row),
        **outreach.contact_summary(row, _today(), cfg),
        "double_up_warning": outreach.double_up_warning(row["company"], cfg),
        "interactions": [dict(i) for i in interactions],
        "suggested_template": outreach.suggest_template(row, cfg),
    }


@_contact_tool
def add_contact(
    track: str,
    name: str | None = None,
    company: str | None = None,
    email: str | None = None,
    linkedin_url: str | None = None,
    kind_detail: str | None = None,
    priority: str | None = None,
    nyc_relevance: str | None = None,
    ask: str | None = None,
    next_step: str | None = None,
    notes: str | None = None,
    source: str | None = None,
    location: str | None = None,
) -> dict:
    """Add a contact. track is agency, company_recruiter, referral, mentor or platform.

    Needs a name or a company, so a firm with no person yet is fine. Never
    invent a name or an email: leave unknown fields out. location is a short
    label for the market they cover, such as NY, AZ or Remote. If the email is
    already saved, nothing is added and the existing contact comes back.
    Returns warnings to show the user.
    """
    cfg = _outreach_cfg()
    conn = _connect()
    try:
        existing = db.fetch_contacts(conn)
        duplicate = outreach.find_duplicate(email, existing)
        if duplicate:
            return {
                "added": False,
                "contact": outreach.contact_summary(duplicate, _today(), cfg),
                "warnings": [f"Already saved as contact {duplicate['id']}"],
            }
        fields, warnings = outreach.plan_new_contact(
            {
                "track": track, "name": name, "company": company, "email": email,
                "linkedin_url": linkedin_url, "kind_detail": kind_detail,
                "priority": priority, "nyc_relevance": nyc_relevance, "ask": ask,
                "next_step": next_step, "notes": notes, "source": source,
                "location": location,
            },
            existing,
            cfg,
        )
        contact_id = db.insert_contact(conn, **fields)
        row = db.get_contact(conn, contact_id)
    finally:
        conn.close()
    return {
        "added": True,
        "contact": outreach.contact_summary(row, _today(), cfg),
        "warnings": warnings,
    }


@_contact_tool
def update_contact(contact_id: int, fields: dict) -> dict:
    """Update a contact: status, dates, next_step, notes, gmail_draft_id and more.

    notes appends and never overwrites. Marking a contact Sent starts the
    follow-up clock, so only do that once the user has actually sent it.
    Saving gmail_draft_id moves a Not started contact to Drafted. Skipped
    contacts come back through restore_contact. Do-not-contact people are
    refused. Returns warnings to show the user.
    """
    cfg = _outreach_cfg()
    conn = _connect()
    try:
        row = _require_contact(conn, contact_id)
        changes, warnings = outreach.plan_update(row, fields, _today(), cfg)
        if "email" in changes:
            others = [c for c in db.fetch_contacts(conn) if c["id"] != contact_id]
            duplicate = outreach.find_duplicate(changes["email"], others)
            if duplicate:
                raise ValueError(f"Email already used by contact {duplicate['id']}")
        return _write_and_read(conn, contact_id, changes, warnings, cfg)
    finally:
        conn.close()


@_contact_tool
def restore_contact(contact_id: int) -> dict:
    """Bring a skipped contact back to Not started so it can be worked again.

    Keeps the original skip reason in the notes. Do-not-contact people are
    refused: taking one off that list is the user's edit in profile.yaml.
    """
    cfg = _outreach_cfg()
    conn = _connect()
    try:
        row = _require_contact(conn, contact_id)
        fields = outreach.restore_fields(row, cfg)
        return _write_and_read(conn, contact_id, fields, [], cfg)
    finally:
        conn.close()


@_contact_tool
def log_interaction(
    contact_id: int,
    channel: str,
    direction: str,
    summary: str,
    date: str | None = None,
) -> dict:
    """Log a message or call, then update the contact's status and last_reply.

    channel is email, linkedin, call or in_person. direction is out or in.
    date is YYYY-MM-DD and defaults to today. An inbound message moves earlier
    statuses to Replied. An outbound one moves the contact along the send
    sequence, and is refused for do-not-contact people.
    """
    outreach.check_interaction(channel, direction, summary)
    cfg = _outreach_cfg()
    on_date = outreach.iso_date(date or _today())
    conn = _connect()
    try:
        row = _require_contact(conn, contact_id)
        # Worked out first, so a refused interaction leaves no log entry.
        fields = outreach.apply_interaction(row, direction, on_date, cfg)
        db.add_interaction(conn, contact_id, on_date, channel, direction, summary.strip())
        return _write_and_read(conn, contact_id, fields, [], cfg)
    finally:
        conn.close()


@_contact_tool
def list_followups_due(as_of: str | None = None) -> list[dict]:
    """Contacts needing action as of a date (default today), oldest first.

    Covers follow-ups due or overdue, replies with no next step, and contacts
    to mark Cold after two follow-ups. needs_action says which.
    """
    cfg = _outreach_cfg()
    day = outreach.iso_date(as_of or _today())
    conn = _connect()
    try:
        rows = db.fetch_contacts(conn)
    finally:
        conn.close()
    return [
        outreach.contact_summary(contact, day, cfg)
        for contact, _ in outreach.follow_ups_due(rows, day, cfg)
    ]


@_contact_tool
def contacts_for_job(app_id: int) -> dict:
    """Active contacts at a tracked job's company, grouped by track.

    referral_path is true only for a real connection: the user's own pipeline
    or someone who has replied. Other contacts are cold leads, not a way in.
    Also includes anyone linked to this application.
    """
    cfg = _outreach_cfg()
    conn = _connect()
    try:
        application = db.get_application(conn, app_id)
        if application is None:
            raise ValueError(f"no application with app_id {app_id}")
        rows = db.fetch_contacts(conn)
        linked = db.linked_contact_ids(conn, app_id)
    finally:
        conn.close()
    return {
        "app_id": app_id,
        **outreach.contacts_for_company(rows, application["company"], linked, _today(), cfg),
    }


@_contact_tool
def link_contact_to_job(contact_id: int, app_id: int, note: str | None = None) -> str:
    """Tie a contact to a specific tracked job, such as a req a recruiter submitted."""
    conn = _connect()
    try:
        _require_contact(conn, contact_id)
        if db.get_application(conn, app_id) is None:
            raise ValueError(f"no application with app_id {app_id}")
        db.link_contact_application(conn, contact_id, app_id, note)
    finally:
        conn.close()
    return f"Linked contact {contact_id} to app_id {app_id}."


@_contact_tool
def list_referral_watch() -> list[dict]:
    """Open roles to send to standing referrals, by contact.

    Covers every contact marked standing_referral. A role stays here until it's
    tied with link_contact_to_job or dismissed in the feed, and leaves by itself
    once the posting is delisted.
    """
    cfg = _outreach_cfg()
    years_cfg = _years_cfg()
    conn = _connect()
    try:
        return referral_watch.find_matches(conn, cfg, years_cfg)
    finally:
        conn.close()


@_contact_tool
def weekly_summary(week_of: str | None = None) -> dict:
    """Pipeline counts, follow-ups due, reply rate and progress for a week.

    week_of is any date in the week, default today. Targets are a guide and
    aren't enforced.
    """
    cfg = _outreach_cfg()
    day = outreach.iso_date(week_of or _today())
    conn = _connect()
    try:
        rows = db.fetch_contacts(conn)
        interactions = db.fetch_interactions(conn)
    finally:
        conn.close()
    return outreach.weekly_summary(rows, interactions, day, cfg)


def _schedule_cfg() -> dict:
    return yaml.safe_load(PROFILE_PATH.read_text())["schedule"]


def _schedule_day(conn, cfg: dict, day: str) -> dict:
    now = datetime.now()
    monday, sunday = schedule.week_bounds(day)
    activity = db.fetch_schedule_activity(conn, monday.isoformat(), sunday.isoformat())
    logs = [dict(r) for r in db.fetch_day_logs(conn, monday.isoformat(), sunday.isoformat())]
    built = schedule.build_day(cfg, day, activity, logs, now)
    week = schedule.build_week(cfg, day, activity, logs, now)
    nxt = schedule.next_block(built["blocks"])
    return {
        "day": built["day"],
        "weekday": built["weekday"],
        "workday": built["workday"],
        "now": now.strftime("%H:%M"),
        "up_next": nxt["key"] if nxt else None,
        "adherence": built["adherence"],
        "blocks": [
            {k: b[k] for k in ("key", "label", "start", "end", "kind", "detail",
                               "status", "count", "target", "evidence", "logged", "note")}
            for b in built["blocks"]
        ],
        "week": {"pct": week["pct"], "targets": week["targets"]},
    }


@_contact_tool
def today_plan(day: str | None = None) -> dict:
    """The day's schedule blocks with status (done/partial/now/upcoming/missed;
    ongoing for the all-day triage block, which is never done or missed),
    the evidence behind each, and this week's targets.

    Applications and outreach blocks fill themselves from the tracker and
    contacts; manual blocks (coding, Hello Interview, shut down) are done only
    when logged with log_block. day defaults to today.
    """
    cfg = _schedule_cfg()
    conn = _connect()
    try:
        return _schedule_day(conn, cfg, outreach.iso_date(day or _today()))
    finally:
        conn.close()


@_contact_tool
def log_block(block_key: str, note: str | None = None, day: str | None = None) -> dict:
    """Mark a schedule block done for a day (default today), with an optional
    one-line note of what was covered. Works for any block; use it after a
    prep session run in chat, or when the user says they did the block.
    Logging twice keeps the block done and appends the note.
    """
    cfg = _schedule_cfg()
    keys = {b["key"] for b in cfg["blocks"]}
    if block_key not in keys:
        raise ValueError(f"no block {block_key!r}; blocks are {sorted(keys)}")
    when = outreach.iso_date(day or _today())
    conn = _connect()
    try:
        db.log_block(conn, when, block_key, (note or "").strip() or None, "claude")
        return _schedule_day(conn, cfg, when)
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
