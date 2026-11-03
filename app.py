"""FastAPI + HTMX UI: matches feed, dismiss, re-score, profile editor.

Rendering is read-only over what Phase 2 stored — the feed's "why
ranked here" block is the score_breakdown JSON verbatim, and the
rejected view is the stored gate_reason strings. No scoring logic here.
"""

import json
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import analytics
import db
import email_sync
import profile_io
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


def get_db():
    conn = db.get_connection(check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def _matches(conn, q: str | None = None) -> list[dict]:
    jobs = []
    for row in db.fetch_matches(conn, q):
        job = dict(row)
        job["breakdown"] = json.loads(job["score_breakdown"]) if job["score_breakdown"] else None
        jobs.append(job)
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
    # Empty 200: the card's hx-swap="delete" removes it from the feed.
    return Response(status_code=200)


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
    return [{"name": lane, "apps": apps} for lane, apps in by_lane.items()]


@app.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "tracker.html", {"lanes": _lanes(conn), "all_lanes": db.LANES}
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
         "lanes": _lanes(conn), "all_lanes": db.LANES},
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
