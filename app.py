"""FastAPI + HTMX UI: matches feed, dismiss, re-score, profile editor.

Rendering is read-only over what Phase 2 stored — the feed's "why
ranked here" block is the score_breakdown JSON verbatim, and the
rejected view is the stored gate_reason strings. No scoring logic here.
"""

import json
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

import analytics
import db
import email_sync
import outreach
import profile_io
import referral_watch
from autosweep import sweep_and_rescore
from scoring.rescore import rescore_all
from scoring.score import score_job
from scrapers import common as scraper_common
from scrapers import lookup
from scrapers.sweep import load_profile

FRONTEND_DIR = Path(__file__).parent / "frontend"
PROFILE_PATH = profile_io.PROFILE_PATH
REJECTED_ROWS_PER_GATE = 100
DISMISSED_ROWS = 100


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.get_connection()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(title="Job Search Tool", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    # StaticFiles sends no Cache-Control, so browsers heuristically cache
    # style.css and serve stale styles after an edit. no-cache keeps the
    # ETag/304 flow but forces revalidation on every load.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response
templates = Jinja2Templates(directory=FRONTEND_DIR)

CONVERSATIONS_DIR = db.CONVERSATIONS_DIR
_CONVERSATION_PATH = re.compile(r"data/conversations/([\w./-]+?\.md)")
# Runs on escaped text, so stop at an entity like &#39;. A bare domain needs a
# path, which keeps words such as Node.js out.
_MESSAGE_LINK = re.compile(
    r"(?P<url>(?:https?://|\b[\w-]+\.(?:com|io|ai|org|net)/)(?:(?!&#)[^\s<])*)"
    r"|(?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
)


def link_conversations(text: str) -> Markup:
    """Notes, escaped, with saved conversation paths turned into links."""
    return Markup(
        _CONVERSATION_PATH.sub(
            lambda m: f'<a href="/conversations/{m[1]}">{m[0]}</a>',
            str(escape(text)),
        )
    )


def _message_link(match: re.Match) -> str:
    """One URL or email as a link, leaving sentence punctuation outside it."""
    if match["email"]:
        return f'<a href="mailto:{match["email"]}">{match["email"]}</a>'
    url = match["url"].rstrip(".,;:!?)]")
    href = url if "://" in url else f"https://{url}"
    tail = match["url"][len(url):]
    return f'<a href="{href}" target="_blank" rel="noopener">{url}</a>{tail}'


def render_conversation(text: str) -> Markup:
    """A saved conversation, escaped, with links, headings and speakers styled."""
    html = _MESSAGE_LINK.sub(_message_link, str(escape(text)))
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.M)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.M)
    html = re.sub(r"^\*\*(.+?)\*\*", r"<strong>\1</strong>", html, flags=re.M)
    return Markup(html)


templates.env.filters["link_conversations"] = link_conversations


def get_db():
    conn = db.get_connection(check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def _outreach_cfg() -> dict:
    return load_profile(PROFILE_PATH).get("outreach") or {}


def _years_cfg() -> dict:
    """The scorer's level bands, so the referral watcher agrees with the feed."""
    return load_profile(PROFILE_PATH)["scoring"]["seniority"]["years_to_level"]


def _today() -> str:
    return date.today().isoformat()


def _attach_contact_badges(conn, rows: list[dict]) -> list[dict]:
    """Label job dicts that have contacts, looking up each company only once."""
    contacts = db.fetch_contacts(conn)
    if not contacts:
        return []
    cfg = _outreach_cfg()
    badges = outreach.company_badges(contacts, cfg)
    by_company: dict[str, str | None] = {}
    for row in rows:
        name = row["company"]
        if name not in by_company:
            by_company[name] = badges.get(outreach.company_key(name, cfg))
        row["contact_badge"] = by_company[name]
    return contacts


def _matches(conn, q: str | None = None) -> list[dict]:
    jobs = []
    for row in db.fetch_matches(conn, q):
        job = dict(row)
        job["breakdown"] = json.loads(job["score_breakdown"]) if job["score_breakdown"] else None
        jobs.append(job)
    _attach_contact_badges(conn, jobs)
    return jobs


def _rescore_summary(summary: dict) -> str:
    return (
        f"Rescored {summary['total']} jobs: {summary['passed']} passed gates, "
        f"{summary['rejected']} rejected."
    )


def _last_sweep(conn) -> dict | None:
    """Local time + rough age of the last sweep, for the feed header."""
    stamp = db.last_sweep_time(conn)
    if not stamp:
        return None
    swept = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    minutes = max(0, int((datetime.now(timezone.utc) - swept).total_seconds() // 60))
    if minutes < 60:
        ago = f"{minutes} min ago"
    elif minutes < 48 * 60:
        ago = f"{minutes // 60} h ago"
    else:
        ago = f"{minutes // (24 * 60)} days ago"
    return {"local": swept.astimezone().strftime("%a %H:%M"), "ago": ago}


def _last_run(conn) -> dict | None:
    """Most recent recorded sweep run, errors JSON parsed, for the feed header."""
    row = db.last_sweep_run(conn)
    if row is None:
        return None
    run = dict(row)
    run["errors"] = json.loads(run["errors"]) if run["errors"] else []
    return run


@app.get("/", response_class=HTMLResponse)
def matches_feed(request: Request, q: str = "", conn=Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"jobs": _matches(conn, q), "summary": None, "q": q,
         "last_sweep": _last_sweep(conn), "last_run": _last_run(conn)},
    )


@app.get("/feed", response_class=HTMLResponse)
def feed_partial(request: Request, q: str = "", conn=Depends(get_db)):
    """HTMX partial for the search box (and anything else refreshing the feed)."""
    return templates.TemplateResponse(
        request, "_feed.html", {"jobs": _matches(conn, q), "summary": None}
    )


@app.post("/jobs/{job_id}/dismiss")
def dismiss(job_id: int, conn=Depends(get_db)) -> Response:
    if not db.dismiss_job(conn, job_id):
        raise HTTPException(status_code=404, detail=f"no job with id {job_id}")
    # Empty 200: the card's hx-swap="delete" removes it from the feed. The header
    # refreshes the Contacts page when the dismiss came from its Due today list.
    return Response(status_code=200, headers=_CONTACTS_CHANGED)


@app.get("/dismissed", response_class=HTMLResponse)
def dismissed(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "dismissed.html",
        {"jobs": [dict(row) for row in db.fetch_dismissed(conn, DISMISSED_ROWS)]},
    )


@app.post("/jobs/{job_id}/restore")
def restore(job_id: int, conn=Depends(get_db)) -> Response:
    if not db.restore_job(conn, job_id):
        raise HTTPException(status_code=404, detail=f"no job with id {job_id}")
    # Empty 200: the row's hx-swap="delete" removes it from the list.
    return Response(status_code=200)


@app.post("/rescore", response_class=HTMLResponse)
async def rescore(request: Request, conn=Depends(get_db)):
    summary = rescore_all(
        conn, load_profile(PROFILE_PATH), now=datetime.now(timezone.utc)
    )
    q = str((await request.form()).get("q", ""))  # keep the active search filter
    return templates.TemplateResponse(
        request,
        "_feed.html",
        {"jobs": _matches(conn, q), "summary": _rescore_summary(summary)},
    )


@app.post("/sweep", response_class=HTMLResponse)
async def sweep_now(request: Request, conn=Depends(get_db)):
    """The UI sweep button: fetch all boards, rescore, refresh the feed.

    Synchronous on purpose — a watchlist sweep takes a few dozen seconds
    (1s delay per company) and this is a single-user local tool; HTMX
    keeps the button disabled and shows an indicator meanwhile.
    """
    summary = sweep_and_rescore(conn, load_profile(PROFILE_PATH))
    swept = summary["sweep"]
    message = (
        f"Swept {swept['companies']} companies: "
        f"{swept['seen']} jobs ({swept['new']} new)"
    )
    if swept["errors"]:
        message += f", {len(swept['errors'])} error{'s' if len(swept['errors']) != 1 else ''}"
    message += ". " + _rescore_summary(summary["rescore"])
    q = str((await request.form()).get("q", ""))  # keep the active search filter
    return templates.TemplateResponse(
        request,
        "_sweep_result.html",
        {"jobs": _matches(conn, q), "summary": message,
         "last_sweep": _last_sweep(conn), "last_run": _last_run(conn)},
    )


def _app_dict(row) -> dict:
    """Application row -> template dict with the breakdown JSON parsed."""
    app_row = dict(row)
    app_row["breakdown"] = (
        json.loads(app_row["score_breakdown"]) if app_row["score_breakdown"] else None
    )
    return app_row


def _sender(item: dict) -> str:
    """'Name <address>', or whichever of the two the row has."""
    name, addr = item.get("from_name"), item.get("from_addr")
    if name and addr:
        return f"{name} <{addr}>"
    return name or addr or "?"


def _ignored_mail(conn) -> dict:
    """Template context for the tracker's Ignored mail list."""
    rows = []
    for row in db.fetch_ignored_emails(conn):
        item = dict(row)
        item["gmail_url"] = email_sync.gmail_link(item["message_id"])
        item["sender"] = _sender(item)
        rows.append(item)
    return {"ignored": rows, "ignored_count": db.count_ignored_emails(conn)}


def _attach_suggestions(conn, apps: list[dict]) -> None:
    """Give each app dict its email activity: `suggestions` (pending,
    actionable box on the card) and `email_replies` (accepted history,
    shown read-only in the details section and modal)."""
    pending: dict[int, list[dict]] = {}
    replies: dict[int, list[dict]] = {}
    for row in db.fetch_email_activity(conn):
        bucket = pending if row["status"] == "pending" else replies
        item = dict(row)
        item["gmail_url"] = email_sync.gmail_link(item["message_id"])
        item["kind_label"] = email_sync.kind_label(item["kind"], item["suggested_lane"])
        item["sender"] = _sender(item)
        bucket.setdefault(item["application_id"], []).append(item)
    for app_row in apps:
        app_row["suggestions"] = pending.get(app_row["id"], [])
        app_row["email_replies"] = replies.get(app_row["id"], [])


def _lanes(conn) -> list[dict]:
    by_lane: dict[str, list[dict]] = {lane: [] for lane in db.LANES}
    all_apps = []
    for row in db.fetch_applications(conn):
        app_row = _app_dict(row)
        by_lane[row["lane"]].append(app_row)
        all_apps.append(app_row)
    _attach_suggestions(conn, all_apps)
    contacts = _attach_contact_badges(conn, all_apps)
    linked = outreach.linked_badges(contacts, db.fetch_contact_links(conn))
    for app_row in all_apps:
        app_row["contact_badge"] = linked.get(app_row["id"]) or app_row.get("contact_badge")
    return [{"name": lane, "apps": apps} for lane, apps in by_lane.items()]


@app.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "tracker.html",
        {"lanes": _lanes(conn), "all_lanes": db.LANES, **_ignored_mail(conn)},
    )


@app.get("/applications/{app_id}/modal", response_class=HTMLResponse)
def application_modal(app_id: int, request: Request, conn=Depends(get_db)):
    """Full-detail modal for a tracker card: JD, breakdown, details, status."""
    row = db.get_application(conn, app_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no application with id {app_id}")
    app_row = _app_dict(row)
    _attach_suggestions(conn, [app_row])
    return templates.TemplateResponse(request, "_app_modal.html", {"a": app_row})


@app.get("/jobs/lookup", response_class=HTMLResponse)
def lookup_job(request: Request, url: str = "", conn=Depends(get_db)):
    """Fetch a pasted ATS posting URL and prefill the add-job form.

    If the posting is already in the database (watched company), offer to
    track the existing job instead of creating a duplicate manual copy.
    """
    def form(**ctx):
        return templates.TemplateResponse(request, "_add_job_form.html", ctx)

    parsed = lookup.parse_job_url(url.strip())
    if parsed is None:
        return form(
            lookup_error="Couldn't recognize that URL — only Greenhouse, Lever and "
            "Ashby job links can be fetched (JSON APIs only, no page scraping). "
            "Fill the form in manually."
        )
    ats_type, slug, external_id = parsed
    existing = db.find_job_by_source(conn, ats_type, slug, external_id)
    if existing is not None:
        return form(existing=dict(existing))
    scraper_cfg = load_profile(PROFILE_PATH)["scraper"]
    try:
        posting = lookup.fetch_posting(
            ats_type,
            slug,
            external_id,
            user_agent=scraper_cfg["user_agent"],
            timeout=scraper_cfg["timeout_seconds"],
        )
    except scraper_common.FetchError as exc:
        return form(lookup_error=f"Fetch failed: {exc}")
    if posting is None:
        return form(lookup_error="That board no longer lists this posting.")
    company = db.company_display_name(conn, ats_type, slug) or slug.replace("-", " ").title()
    return form(
        v={
            "company": company,
            "title": posting["title"],
            "url": posting["url"] or url.strip(),
            "location": posting["location"] or "",
            "description": posting["description"] or "",
        },
        lookup_note=f"Fetched from {ats_type} — review, then add.",
    )


@app.post("/jobs/manual")
def add_manual_job(
    company: str = Form(...),
    title: str = Form(...),
    url: str = Form(""),
    location: str = Form(""),
    description: str = Form(""),
    conn=Depends(get_db),
):
    company, title = company.strip(), title.strip()
    if not company or not title:
        raise HTTPException(status_code=422, detail="company and title are required")
    url = url.strip()
    if url and "://" not in url:
        url = f"https://{url}"
    description = description.strip() or None
    # Scored like any match (no gates — a hand-added job is wanted by
    # definition); rescores keep it fresh after profile edits.
    result = score_job(
        {"title": title, "description": description}, load_profile(PROFILE_PATH)["scoring"]
    )
    db.create_manual_job(
        conn,
        company=company,
        title=title,
        url=url or None,
        location=location.strip() or None,
        description=description,
        score=result.score,
        score_breakdown=json.dumps(result.breakdown()),
    )
    # Plain form POST: land on the board with the new card in considering.
    return RedirectResponse("/tracker", status_code=303)


@app.post("/jobs/{job_id}/track", response_class=HTMLResponse)
def track(job_id: int, conn=Depends(get_db)):
    try:
        db.create_application(conn, job_id)  # None (already tracked) is fine
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=404, detail=f"no job with id {job_id}")
    # A tracked job always gets a fit score, even if gate-rejected (gates
    # decide feed entry, not whether a job you chose deserves a number).
    # Reached via the URL-lookup flow; feed cards are always scored already.
    row = conn.execute(
        """SELECT j.score, j.title, j.description, c.ats_type, c.slug
           FROM jobs j JOIN companies c ON c.id = j.company_id WHERE j.id = ?""",
        (job_id,),
    ).fetchone()
    if row["score"] is None:
        profile = load_profile(PROFILE_PATH)
        weight = next(
            (e.get("weight") for e in profile["companies"]
             if e["ats"] == row["ats_type"] and e["slug"] == row["slug"]),
            None,
        )
        job_dict = {"title": row["title"], "description": row["description"],
                    "company_weight": weight}
        result = score_job(job_dict, profile["scoring"])
        conn.execute(
            "UPDATE jobs SET score = ?, score_breakdown = ? WHERE id = ?",
            (result.score, json.dumps(result.breakdown()), job_id),
        )
        conn.commit()
    # Swaps in for the card's Track button (hx-swap="outerHTML"). New
    # applications always start in the considering lane.
    return '<a class="tracked" href="/tracker">Considering</a>'


@app.post("/applications/{app_id}/lane", response_class=HTMLResponse)
def move_lane(
    app_id: int, request: Request, lane: str = Form(...), conn=Depends(get_db)
):
    if lane not in db.LANES:
        raise HTTPException(status_code=422, detail=f"unknown lane {lane!r}")
    if not db.set_application_lane(conn, app_id, lane):
        raise HTTPException(status_code=404, detail=f"no application with id {app_id}")
    return templates.TemplateResponse(
        request, "_board.html", {"lanes": _lanes(conn), "all_lanes": db.LANES}
    )


@app.post("/applications/{app_id}/untrack", response_class=HTMLResponse)
def untrack(app_id: int, request: Request, conn=Depends(get_db)):
    try:
        found = db.untrack_application(conn, app_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not found:
        raise HTTPException(status_code=404, detail=f"no application with id {app_id}")
    return templates.TemplateResponse(
        request, "_board.html", {"lanes": _lanes(conn), "all_lanes": db.LANES}
    )


@app.post("/applications/{app_id}", response_class=HTMLResponse)
async def save_application(app_id: int, request: Request, conn=Depends(get_db)):
    form = await request.form()
    fields = {
        key: str(form.get(key, "")).strip() or None
        for key in ("notes", "resume_version", "next_action", "next_action_due", "applied_at")
    }
    if not db.update_application_details(conn, app_id, **fields):
        raise HTTPException(status_code=404, detail=f"no application with id {app_id}")
    app_row = _app_dict(db.get_application(conn, app_id))
    _attach_suggestions(conn, [app_row])
    return templates.TemplateResponse(
        request,
        "_app_card.html",
        {"a": app_row, "all_lanes": db.LANES, "saved": True},
    )


@app.post("/email/sync", response_class=HTMLResponse)
def email_sync_now(request: Request, conn=Depends(get_db)):
    """The tracker's Sync email button. Synchronous like /sweep; on
    success the response also refreshes the board out-of-band so new
    suggestions appear on their cards."""
    profile = load_profile(PROFILE_PATH)
    cfg = profile.get("email")
    secrets = email_sync.load_secrets()
    if not cfg or not cfg.get("enabled") or secrets is None:
        return templates.TemplateResponse(
            request,
            "_email_sync_status.html",
            {"error": "Email sync isn't configured — copy "
                      "config/secrets.yaml.example to config/secrets.yaml and "
                      "fill in your Gmail address and app password.",
             "lanes": None},
        )
    try:
        summary = email_sync.sync(conn, profile, secrets=secrets)
    except Exception as exc:  # IMAP/auth errors belong in the UI, not a 500
        return templates.TemplateResponse(
            request,
            "_email_sync_status.html",
            {"error": f"Email sync failed: {exc}", "lanes": None},
        )
    return templates.TemplateResponse(
        request,
        "_email_sync_status.html",
        {"message": email_sync.format_summary(summary), "error": None,
         "lanes": _lanes(conn), "all_lanes": db.LANES, **_ignored_mail(conn)},
    )


@app.post("/email/suggestions/{sid}/accept", response_class=HTMLResponse)
def accept_suggestion(sid: int, request: Request, conn=Depends(get_db)):
    """Apply a suggestion and keep it as reply history.

    With a suggested lane this moves the application (normal event
    logging); a lane-less "reply received" is simply kept — accepted
    rows render in the card's details section and the modal."""
    row = db.get_email_suggestion(conn, sid)
    if row is None or row["status"] != "pending":
        raise HTTPException(status_code=404, detail=f"no pending suggestion {sid}")
    if row["suggested_lane"] is not None:
        db.set_application_lane(conn, row["application_id"], row["suggested_lane"])
    db.set_suggestion_status(conn, sid, "accepted")
    return templates.TemplateResponse(
        request, "_board.html", {"lanes": _lanes(conn), "all_lanes": db.LANES}
    )


@app.post("/email/suggestions/{sid}/dismiss", response_class=HTMLResponse)
def dismiss_suggestion(sid: int, request: Request, conn=Depends(get_db)):
    row = db.get_email_suggestion(conn, sid)
    if row is None or row["status"] != "pending":
        raise HTTPException(status_code=404, detail=f"no pending suggestion {sid}")
    db.set_suggestion_status(conn, sid, "dismissed")
    return templates.TemplateResponse(
        request, "_board.html", {"lanes": _lanes(conn), "all_lanes": db.LANES}
    )


# Edit-form fields, compared against the contact so only real changes are saved.
_CONTACT_FORM_FIELDS = (
    "status", "priority", "name", "company", "email", "linkedin_url",
    "kind_detail", "ask", "next_step", "date_contacted", "last_reply",
    "verify_flag", "location",
)
_CONTACTS_CHANGED = {"HX-Trigger": "contactsChanged"}


def _contact_view(row, cfg: dict) -> dict:
    return {**dict(row), **outreach.contact_summary(row, _today(), cfg)}


def _contacts_context(conn, cfg: dict, params: dict) -> dict:
    """Everything the page body shows, for one set of filters."""
    rows = db.fetch_contacts(conn)
    today = _today()
    skipped_tab = params.get("tab") == "skipped"
    if skipped_tab:
        shown = outreach.filter_contacts(
            rows, cfg, status="Skip", track=params.get("track") or None,
            search=params.get("q") or None, location=params.get("location") or None,
        )
    else:
        shown = outreach.visible_contacts(
            rows, cfg,
            track=params.get("track") or None,
            status=params.get("status") or None,
            priority=params.get("priority") or None,
            search=params.get("q") or None,
            show_c=bool(params.get("show_c")),
            location=params.get("location") or None,
        )
    views = {c["id"]: _contact_view(c, cfg) for c in rows}
    return {
        "tab": "skipped" if skipped_tab else "active",
        "params": params,
        "locations": outreach.contact_locations(rows),
        "groups": [
            (track, [views[c["id"]] for c in group])
            for track, group in outreach.group_by_track(shown)
        ],
        "shown_count": len(shown),
        "active_count": sum(1 for c in rows if c["status"] != "Skip"),
        "skipped_count": sum(1 for c in rows if c["status"] == "Skip"),
        "due": [views[c["id"]] for c, _ in outreach.follow_ups_due(rows, today, cfg)],
        "watch": referral_watch.find_matches(conn, cfg, _years_cfg()),
        "week": outreach.weekly_summary(rows, db.fetch_interactions(conn), today, cfg),
    }


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, conn=Depends(get_db)):
    params = dict(request.query_params)
    return templates.TemplateResponse(
        request,
        "contacts.html",
        {
            **_contacts_context(conn, _outreach_cfg(), params),
            "statuses": db.CONTACT_STATUSES,
            "priorities": db.CONTACT_PRIORITIES,
            "tracks": outreach.TRACK_ORDER,
            "today": _today(),
        },
    )


@app.get("/contacts/body", response_class=HTMLResponse)
def contacts_body(request: Request, conn=Depends(get_db)):
    """HTMX partial: filters changed, or a contact was edited."""
    params = dict(request.query_params)
    return templates.TemplateResponse(
        request, "_contacts_body.html", _contacts_context(conn, _outreach_cfg(), params)
    )


@app.get("/contacts/due-count", response_class=HTMLResponse)
def contacts_due_count(conn=Depends(get_db)):
    """Nav badge. Empty when nothing is due, so CSS can hide it."""
    cfg = _outreach_cfg()
    due = outreach.follow_ups_due(db.fetch_contacts(conn), _today(), cfg)
    total = len(due) + len(referral_watch.find_matches(conn, cfg, _years_cfg()))
    return HTMLResponse(str(total) if total else "")


def _render_contact_modal(request, conn, contact_id, cfg, *, warnings=(), error=None,
                          headers=None):
    row = db.get_contact(conn, contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no contact with id {contact_id}")
    return templates.TemplateResponse(
        request,
        "_contact_modal.html",
        {
            "c": _contact_view(row, cfg),
            "interactions": db.fetch_interactions(conn, contact_id),
            "template": outreach.suggest_template(row, cfg),
            "double_up": outreach.double_up_warning(row["company"], cfg),
            "warnings": warnings,
            "error": error,
            "statuses": db.CONTACT_STATUSES,
            "priorities": db.CONTACT_PRIORITIES,
            "channels": db.INTERACTION_CHANNELS,
            "today": _today(),
        },
        headers=headers,
    )


@app.get("/conversations/{name:path}", response_class=HTMLResponse)
def conversation_page(name: str, request: Request):
    """One saved conversation. Only .md files under CONVERSATIONS_DIR."""
    root = CONVERSATIONS_DIR.resolve()
    path = (root / name).resolve()
    if path.suffix != ".md" or not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="no such conversation")
    return templates.TemplateResponse(
        request,
        "conversation.html",
        {"body": render_conversation(path.read_text(encoding="utf-8")), "title": path.stem},
    )


@app.get("/contacts/{contact_id}/modal", response_class=HTMLResponse)
def contact_modal(contact_id: int, request: Request, conn=Depends(get_db)):
    return _render_contact_modal(request, conn, contact_id, _outreach_cfg())


@app.post("/contacts/{contact_id}", response_class=HTMLResponse)
async def contact_update(contact_id: int, request: Request, conn=Depends(get_db)):
    """Save the modal's edit form. Refusals come back inline with the reason."""
    cfg = _outreach_cfg()
    row = db.get_contact(conn, contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no contact with id {contact_id}")
    form = await request.form()
    changes = {}
    for field in _CONTACT_FORM_FIELDS:
        if field in form:
            value = form[field].strip() or None
            if value != row[field]:
                changes[field] = value
    if "standing_referral" in form:
        wanted = int(form["standing_referral"])
        if wanted != row["standing_referral"]:
            changes["standing_referral"] = wanted
    if form.get("notes", "").strip():
        changes["notes"] = form["notes"]
    try:
        fields, warnings = outreach.plan_update(row, changes, _today(), cfg)
        if "email" in fields:
            others = [c for c in db.fetch_contacts(conn) if c["id"] != contact_id]
            duplicate = outreach.find_duplicate(fields["email"], others)
            if duplicate:
                raise ValueError(f"Email already used by contact {duplicate['id']}")
    except ValueError as exc:
        return _render_contact_modal(request, conn, contact_id, cfg, error=str(exc))
    db.update_contact(conn, contact_id, **fields)
    return _render_contact_modal(
        request, conn, contact_id, cfg, warnings=warnings, headers=_CONTACTS_CHANGED
    )


@app.post("/contacts/{contact_id}/interactions", response_class=HTMLResponse)
def contact_log_interaction(
    contact_id: int,
    request: Request,
    channel: str = Form(...),
    direction: str = Form(...),
    summary: str = Form(""),
    on_date: str = Form(""),
    conn=Depends(get_db),
):
    cfg = _outreach_cfg()
    row = db.get_contact(conn, contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no contact with id {contact_id}")
    try:
        outreach.check_interaction(channel, direction, summary)
        day = outreach.iso_date(on_date.strip() or _today())
        # Worked out first, so a refused interaction leaves no log entry.
        fields = outreach.apply_interaction(row, direction, day, cfg)
    except ValueError as exc:
        return _render_contact_modal(request, conn, contact_id, cfg, error=str(exc))
    db.add_interaction(conn, contact_id, day, channel, direction, summary.strip())
    db.update_contact(conn, contact_id, **fields)
    return _render_contact_modal(request, conn, contact_id, cfg, headers=_CONTACTS_CHANGED)


@app.post("/contacts/{contact_id}/seen")
def contact_seen(contact_id: int, conn=Depends(get_db)):
    """Dismiss a new-message flag from Due today."""
    if not db.update_contact(conn, contact_id, new_message=None):
        raise HTTPException(status_code=404, detail=f"no contact with id {contact_id}")
    return Response(status_code=200, headers=_CONTACTS_CHANGED)


@app.post("/contacts/{contact_id}/restore")
def contact_restore(contact_id: int, conn=Depends(get_db)) -> Response:
    row = db.get_contact(conn, contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no contact with id {contact_id}")
    try:
        fields = outreach.restore_fields(row, _outreach_cfg())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.update_contact(conn, contact_id, **fields)
    return Response(status_code=200, headers=_CONTACTS_CHANGED)


@app.post("/contacts/{contact_id}/remove")
def contact_remove(contact_id: int, conn=Depends(get_db)) -> Response:
    """Soft removal, so it's allowed for Skip and do-not-contact people too:
    taking someone off the list isn't contacting them."""
    if not db.remove_contact(conn, contact_id):
        raise HTTPException(status_code=404, detail=f"no contact with id {contact_id}")
    return Response(status_code=200, headers=_CONTACTS_CHANGED)


@app.post("/contacts", response_class=HTMLResponse)
def contact_add(
    request: Request,
    track: str = Form(...),
    source: str = Form("linkedin"),
    name: str = Form(""),
    company: str = Form(""),
    email: str = Form(""),
    linkedin_url: str = Form(""),
    kind_detail: str = Form(""),
    priority: str = Form(""),
    next_step: str = Form(""),
    notes: str = Form(""),
    location: str = Form(""),
    conn=Depends(get_db),
):
    cfg = _outreach_cfg()
    existing = db.fetch_contacts(conn)
    duplicate = outreach.find_duplicate(email, existing)
    if duplicate:
        return templates.TemplateResponse(
            request,
            "_contact_flash.html",
            {"error": f"Already saved: {duplicate['name'] or duplicate['company']}."},
        )
    submitted = {
        "track": track, "source": source, "name": name, "company": company,
        "email": email, "linkedin_url": linkedin_url, "kind_detail": kind_detail,
        "priority": priority, "next_step": next_step, "notes": notes,
        "location": location,
    }
    try:
        fields, warnings = outreach.plan_new_contact(
            {k: v.strip() or None for k, v in submitted.items()}, existing, cfg
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_contact_flash.html", {"error": str(exc)}
        )
    db.insert_contact(conn, **fields)
    return templates.TemplateResponse(
        request,
        "_contact_flash.html",
        {"saved": name.strip() or company.strip(), "warnings": warnings},
        headers=_CONTACTS_CHANGED,
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, conn=Depends(get_db)):
    """Read-only aggregation over applications/events; no LLM, no writes."""
    profile = load_profile(PROFILE_PATH)
    cfg = profile["analytics"]
    scoring_cfg = profile["scoring"]
    data = analytics.build(
        [dict(row) for row in db.fetch_analytics_applications(conn)],
        [dict(row) for row in db.fetch_lane_changes(conn)],
        lanes=db.LANES,
        keywords=scoring_cfg["required_keywords"] + scoring_cfg["preferred_keywords"],
        band_edges=cfg["score_band_edges"],
        fast_hours=cfg["fast_apply_hours"],
    )
    return templates.TemplateResponse(request, "analytics.html", data)


@app.get("/profile", response_class=HTMLResponse)
def profile_editor(request: Request):
    return templates.TemplateResponse(
        request, "profile.html", {"v": _editor_values(), "message": None, "error": None}
    )


@app.post("/profile", response_class=HTMLResponse)
async def profile_save(request: Request, conn=Depends(get_db)):
    form = {key: str(value) for key, value in (await request.form()).items()}
    try:
        edits = profile_io.parse_form(form)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "profile.html",
            {"v": _editor_values(), "message": None, "error": str(exc)},
            status_code=422,
        )
    doc = profile_io.load_round_trip(PROFILE_PATH)
    profile_io.apply_edits(doc, edits)
    profile_io.save_round_trip(doc, PROFILE_PATH)
    summary = rescore_all(
        conn, load_profile(PROFILE_PATH), now=datetime.now(timezone.utc)
    )
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "v": _editor_values(),
            "message": f"Saved. {_rescore_summary(summary)}",
            "error": None,
        },
    )


def _editor_values() -> dict[str, object]:
    """Current profile values keyed by dotted path; lists as textarea text."""
    doc = profile_io.load_round_trip(PROFILE_PATH)
    values: dict[str, object] = {}
    for path, kind in profile_io.FIELDS.items():
        value = profile_io.get_value(doc, path)
        if kind == "list":
            values[path] = "\n".join(value)
        elif kind == "companies":
            values[path] = "\n".join(
                f"{e['name']} | {e['ats']} | {e['slug']}"
                + (f" | {e['weight']:g}" if "weight" in e else "")
                for e in value
            )
        else:
            values[path] = value
    return values


# Reasons are joined with "; ", but a location value can itself contain
# "; " — only a fragment that opens with a gate-name prefix starts a new
# reason.
_GATE_PREFIX = re.compile(r"^[a-z][a-z-]*: ")


def _split_reasons(gate_reason: str) -> list[str]:
    reasons: list[str] = []
    for fragment in gate_reason.split("; "):
        if _GATE_PREFIX.match(fragment) or not reasons:
            reasons.append(fragment)
        else:
            reasons[-1] += "; " + fragment
    return reasons


def _rejected_groups(conn, q: str) -> list[dict]:
    # A job failing several gates appears under each — this is a per-gate
    # audit of reasons, not a partition of jobs.
    by_gate: dict[str, list[dict]] = {}
    for row in db.fetch_rejected(conn, q or None):
        for reason in _split_reasons(row["gate_reason"] or ""):
            gate = reason.split(":", 1)[0]
            by_gate.setdefault(gate, []).append({**dict(row), "reason": reason})
    return [
        {
            "gate": gate,
            "total": len(rows),
            "rows": rows[:REJECTED_ROWS_PER_GATE],
        }
        for gate, rows in sorted(by_gate.items(), key=lambda kv: -len(kv[1]))
    ]


@app.get("/rejected", response_class=HTMLResponse)
def rejected(request: Request, q: str = "", conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "rejected.html", {"groups": _rejected_groups(conn, q), "q": q}
    )


@app.get("/rejected/groups", response_class=HTMLResponse)
def rejected_groups_partial(request: Request, q: str = "", conn=Depends(get_db)):
    """HTMX partial for the company filter box."""
    return templates.TemplateResponse(
        request, "_rejected_groups.html", {"groups": _rejected_groups(conn, q), "q": q}
    )


@app.get("/health")
def health(conn=Depends(get_db)) -> dict:
    return {"status": "ok", "tables": db.table_names(conn)}
