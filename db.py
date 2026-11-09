"""SQLite schema and connection helpers. Plain SQL, no ORM."""

import json
import re
import sqlite3
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "jobs.db"
# Saved message history, git-ignored with the rest of data/.
CONVERSATIONS_DIR = DB_PATH.parent / "conversations"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    -- 'manual' = jobs added by hand, never swept, gated, or scored.
    ats_type    TEXT NOT NULL CHECK (ats_type IN ('greenhouse', 'lever', 'ashby', 'manual')),
    slug        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (ats_type, slug)
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    external_id     TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT,
    department      TEXT,
    url             TEXT,
    description     TEXT,
    posted_at       TEXT,
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen       TEXT NOT NULL DEFAULT (datetime('now')),
    -- Scoring columns stay NULL until Phase 2.
    score           REAL,
    score_breakdown TEXT,
    gate_failed     INTEGER,
    gate_reason     TEXT,
    dismissed       INTEGER NOT NULL DEFAULT 0,
    dismissed_at    TEXT,
    UNIQUE (company_id, external_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    lane            TEXT NOT NULL DEFAULT 'considering' CHECK (lane IN
                    ('considering', 'ready', 'applied', 'interviewing',
                     'offer', 'rejected', 'skipped')),
    resume_version  TEXT,
    notes           TEXT,
    next_action     TEXT,
    next_action_due TEXT,
    applied_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    type            TEXT NOT NULL,
    detail          TEXT,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Inbox → tracker lane suggestions from email sync. One row per
-- (application, mail UID); dismissed rows keep the UID so a re-sync
-- never re-suggests the same message.
CREATE TABLE IF NOT EXISTS email_suggestions (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    msg_uid         TEXT NOT NULL,
    from_addr       TEXT,
    subject         TEXT,
    received_at     TEXT,
    -- Full plain-text body and RFC 822 Message-ID, kept so the mail can
    -- be reread and deep-linked (Gmail rfc822msgid search) later.
    body            TEXT,
    message_id      TEXT,
    -- Display name of the sender, what the mail is (rejected, offer,
    -- interviewing, receipt, person, reply) and the sentence that decided
    -- it. NULL on rows from before these columns existed.
    from_name       TEXT,
    kind            TEXT,
    evidence        TEXT,
    -- NULL = "reply received", no lane change suggested.
    suggested_lane  TEXT CHECK (suggested_lane IS NULL OR suggested_lane IN
                    ('considering', 'ready', 'applied', 'interviewing',
                     'offer', 'rejected')),
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                    ('pending', 'accepted', 'dismissed')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (application_id, msg_uid)
);

-- Mail the sync filtered out on purpose (verification codes, account
-- mail, newsletters), kept so a bad filter can be noticed. No body: the
-- subject, sender and reason are enough, and codes shouldn't be stored.
CREATE TABLE IF NOT EXISTS email_ignored (
    id              INTEGER PRIMARY KEY,
    msg_uid         TEXT NOT NULL UNIQUE,
    company         TEXT,
    from_name       TEXT,
    from_addr       TEXT,
    subject         TEXT,
    received_at     TEXT,
    message_id      TEXT,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every message contact_sync has handled, so none is logged twice.
CREATE TABLE IF NOT EXISTS contact_mail (
    message_id  TEXT PRIMARY KEY,
    contact_id  INTEGER,
    outcome     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per sweep+rescore run (launchd or the UI button), so the feed
-- can show what the most recent sweep actually did.
CREATE TABLE IF NOT EXISTS sweep_runs (
    id               INTEGER PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT NOT NULL DEFAULT (datetime('now')),
    companies        INTEGER NOT NULL,
    jobs_seen        INTEGER NOT NULL,
    jobs_new         INTEGER NOT NULL,
    errors           TEXT,  -- JSON list of per-company error strings
    rescore_total    INTEGER,
    rescore_passed   INTEGER,
    rescore_rejected INTEGER
);

-- Outreach CRM: recruiter and referral contacts. name and company are
-- nullable for firm-only rows; follow-up dates are computed by outreach.py,
-- never stored. peer_history is someone else's experience with the contact,
-- kept as reference and never counted as the user's own activity.
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY,
    name            TEXT,
    company         TEXT,
    email           TEXT,
    linkedin_url    TEXT,
    track           TEXT NOT NULL CHECK (track IN
                    ('agency', 'company_recruiter', 'referral', 'mentor',
                     'platform')),
    kind_detail     TEXT,
    priority        TEXT CHECK (priority IS NULL OR priority IN
                    ('A', 'B', 'C', 'skip')),
    nyc_relevance   TEXT,
    ask             TEXT,
    status          TEXT NOT NULL DEFAULT 'Not started' CHECK (status IN
                    ('Not started', 'Drafted', 'Sent', 'Follow-up 1 sent',
                     'Follow-up 2 sent', 'Replied', 'Call scheduled',
                     'Referred / submitted', 'Cold', 'Skip')),
    date_contacted  TEXT,
    last_reply      TEXT,
    next_step       TEXT,
    notes           TEXT,
    source          TEXT NOT NULL CHECK (source IN
                    ('own_pipeline', 'peer_list', 'research', 'linkedin')),
    peer_history    TEXT,
    verify_flag     TEXT,
    gmail_draft_id  TEXT,
    -- 1 = this person will refer the user whenever there's a role, so their
    -- company is watched for matching postings.
    standing_referral INTEGER NOT NULL DEFAULT 0,
    location        TEXT,  -- the market they cover: NY, AZ, Remote
    new_message     TEXT,  -- day of an inbound message you haven't dealt with
    -- Set when a contact is taken out of the tool. The row stays so the
    -- removal can be undone and a re-import won't bring them back.
    removed_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only outreach log.
CREATE TABLE IF NOT EXISTS interactions (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id),
    date        TEXT NOT NULL,
    channel     TEXT NOT NULL CHECK (channel IN
                ('email', 'linkedin', 'call', 'in_person')),
    direction   TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    summary     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Daily schedule check-offs (/schedule). One row per block per day; the
-- plan itself lives in profile.yaml schedule:, so a row only says "done".
-- Blocks the app can see evidence for (applications, outreach) need no row.
CREATE TABLE IF NOT EXISTS day_log (
    id          INTEGER PRIMARY KEY,
    day         TEXT NOT NULL,
    block_key   TEXT NOT NULL,
    note        TEXT,
    source      TEXT NOT NULL CHECK (source IN ('ui', 'claude')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (day, block_key)
);

-- Explicit link for when a recruiter submits the user to a specific req.
CREATE TABLE IF NOT EXISTS contact_applications (
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (contact_id, application_id)
);
"""


# Kanban lane order for the tracker UI. Must match the applications.lane
# CHECK constraint above.
LANES = (
    "considering",
    # Resume rendered, application not yet submitted on the site —
    # resume_build moves cards here automatically after a render.
    "ready",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    # Terminal "evaluated, decided not to pursue" state from the apply
    # workflow's Stage 2 hiring screen. Last so the active pipeline
    # stays left on the board.
    "skipped",
)


# Outreach enums. Must match the contacts CHECK constraints above.
CONTACT_TRACKS = ("agency", "company_recruiter", "referral", "mentor", "platform")
CONTACT_PRIORITIES = ("A", "B", "C", "skip")
CONTACT_STATUSES = (
    "Not started",
    "Drafted",
    "Sent",
    "Follow-up 1 sent",
    "Follow-up 2 sent",
    "Replied",
    "Call scheduled",
    "Referred / submitted",
    "Cold",
    "Skip",
)
CONTACT_SOURCES = ("own_pipeline", "peer_list", "research", "linkedin")
INTERACTION_CHANNELS = ("email", "linkedin", "call", "in_person")
INTERACTION_DIRECTIONS = ("out", "in")
# Everything insert_contact/update_contact may write.
_CONTACT_FIELDS = (
    "name", "company", "email", "linkedin_url", "track", "kind_detail",
    "priority", "nyc_relevance", "ask", "status", "date_contacted",
    "last_reply", "next_step", "notes", "source", "peer_history",
    "verify_flag", "gmail_draft_id", "standing_referral", "location",
    "new_message",
)


def get_connection(
    db_path: Path = DB_PATH, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    # check_same_thread=False is for FastAPI's per-request connections:
    # the threadpool may run a dependency and its endpoint on different
    # threads, but each connection is still used by one request at a time.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to call on every startup."""
    conn.executescript(SCHEMA)
    # Additive migration for DBs created before dismissed_at existed
    # (CREATE TABLE IF NOT EXISTS never touches an existing table).
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "dismissed_at" not in job_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN dismissed_at TEXT")
    sugg_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(email_suggestions)")
    }
    for col in ("body", "message_id"):
        if col not in sugg_columns:
            conn.execute(f"ALTER TABLE email_suggestions ADD COLUMN {col} TEXT")
    conn.commit()
    _migrate_companies_check(conn)
    _migrate_applications_check(conn)
    _migrate_email_suggestions_check(conn)
    # After the rebuild above, whose table copy doesn't carry these.
    sugg_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(email_suggestions)")
    }
    for col in ("from_name", "kind", "evidence"):
        if col not in sugg_columns:
            conn.execute(f"ALTER TABLE email_suggestions ADD COLUMN {col} TEXT")
    conn.commit()
    _migrate_contacts_check(conn)
    contact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(contacts)")}
    if "standing_referral" not in contact_columns:
        conn.execute(
            "ALTER TABLE contacts ADD COLUMN standing_referral INTEGER NOT NULL DEFAULT 0"
        )
    if "location" not in contact_columns:
        conn.execute("ALTER TABLE contacts ADD COLUMN location TEXT")
    if "new_message" not in contact_columns:
        conn.execute("ALTER TABLE contacts ADD COLUMN new_message TEXT")
        conn.commit()
    # Long descriptions sit before these columns in every row, so a table scan
    # walks about 330 MB; these keep the feed and its header off the rows.
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if {"score", "gate_failed", "last_seen", "company_id"} <= job_columns:
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_jobs_feed
                ON jobs(score DESC, id, company_id)
                WHERE gate_failed = 0 AND dismissed = 0 AND score IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen, company_id);
        """)


def _migrate_companies_check(conn: sqlite3.Connection) -> None:
    """Rebuild companies if its CHECK predates the 'manual' ats_type.

    SQLite can't alter a CHECK constraint, so this is the standard
    copy-drop-rename dance with foreign keys off; jobs.company_id ids are
    preserved. No-op once the constraint mentions 'manual'.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'companies'"
    ).fetchone()
    if row is None or "'manual'" in row["sql"]:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE companies_new (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            ats_type    TEXT NOT NULL CHECK (ats_type IN ('greenhouse', 'lever', 'ashby', 'manual')),
            slug        TEXT NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (ats_type, slug)
        )
        """
    )
    conn.execute(
        "INSERT INTO companies_new SELECT id, name, ats_type, slug, active, created_at FROM companies"
    )
    conn.execute("DROP TABLE companies")
    conn.execute("ALTER TABLE companies_new RENAME TO companies")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_applications_check(conn: sqlite3.Connection) -> None:
    """Rebuild applications if its lane CHECK predates the 'ready' lane
    (which arrived together with dropping 'screening'/'ghosted').

    Same copy-drop-rename dance as _migrate_companies_check; application
    ids are preserved so events and email_suggestions stay attached. Rows
    in the dropped lanes map to their nearest survivor (screening ->
    interviewing, ghosted -> rejected) so the copy can't violate the new
    CHECK.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'applications'"
    ).fetchone()
    if row is None or "'ready'" in row["sql"]:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE applications_new (
            id              INTEGER PRIMARY KEY,
            job_id          INTEGER NOT NULL REFERENCES jobs(id),
            lane            TEXT NOT NULL DEFAULT 'considering' CHECK (lane IN
                            ('considering', 'ready', 'applied', 'interviewing',
                             'offer', 'rejected', 'skipped')),
            resume_version  TEXT,
            notes           TEXT,
            next_action     TEXT,
            next_action_due TEXT,
            applied_at      TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """INSERT INTO applications_new
           SELECT id, job_id,
                  CASE lane WHEN 'screening' THEN 'interviewing'
                            WHEN 'ghosted' THEN 'rejected'
                            ELSE lane END,
                  resume_version, notes, next_action,
                  next_action_due, applied_at, created_at, updated_at
           FROM applications"""
    )
    conn.execute("DROP TABLE applications")
    conn.execute("ALTER TABLE applications_new RENAME TO applications")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_email_suggestions_check(conn: sqlite3.Connection) -> None:
    """Rebuild email_suggestions if its suggested_lane CHECK still allows
    the dropped 'screening'/'ghosted' lanes. Suggestion ids are preserved;
    dropped-lane suggestions map like the applications migration.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'email_suggestions'"
    ).fetchone()
    if row is None or "'ghosted'" not in row["sql"]:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE email_suggestions_new (
            id              INTEGER PRIMARY KEY,
            application_id  INTEGER NOT NULL REFERENCES applications(id),
            msg_uid         TEXT NOT NULL,
            from_addr       TEXT,
            subject         TEXT,
            received_at     TEXT,
            body            TEXT,
            message_id      TEXT,
            suggested_lane  TEXT CHECK (suggested_lane IS NULL OR suggested_lane IN
                            ('considering', 'ready', 'applied', 'interviewing',
                             'offer', 'rejected')),
            reason          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                            ('pending', 'accepted', 'dismissed')),
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (application_id, msg_uid)
        )
        """
    )
    conn.execute(
        """INSERT INTO email_suggestions_new
           SELECT id, application_id, msg_uid, from_addr, subject, received_at,
                  body, message_id,
                  CASE suggested_lane WHEN 'screening' THEN 'interviewing'
                                      WHEN 'ghosted' THEN 'rejected'
                                      ELSE suggested_lane END,
                  reason, status, created_at
           FROM email_suggestions"""
    )
    conn.execute("DROP TABLE email_suggestions")
    conn.execute("ALTER TABLE email_suggestions_new RENAME TO email_suggestions")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_contacts_check(conn: sqlite3.Connection) -> None:
    """Rebuild contacts if it predates the 'mentor' track and removed_at.

    Same copy-drop-rename dance as the other migrations; contact ids are
    preserved so interactions and contact_applications stay attached. The
    email index lives here, not in SCHEMA, because on an older table it would
    name a column that doesn't exist yet.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'contacts'"
    ).fetchone()
    if "'mentor'" not in row["sql"]:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            CREATE TABLE contacts_new (
                id              INTEGER PRIMARY KEY,
                name            TEXT,
                company         TEXT,
                email           TEXT,
                linkedin_url    TEXT,
                track           TEXT NOT NULL CHECK (track IN
                                ('agency', 'company_recruiter', 'referral',
                                 'mentor', 'platform')),
                kind_detail     TEXT,
                priority        TEXT CHECK (priority IS NULL OR priority IN
                                ('A', 'B', 'C', 'skip')),
                nyc_relevance   TEXT,
                ask             TEXT,
                status          TEXT NOT NULL DEFAULT 'Not started' CHECK (status IN
                                ('Not started', 'Drafted', 'Sent', 'Follow-up 1 sent',
                                 'Follow-up 2 sent', 'Replied', 'Call scheduled',
                                 'Referred / submitted', 'Cold', 'Skip')),
                date_contacted  TEXT,
                last_reply      TEXT,
                next_step       TEXT,
                notes           TEXT,
                source          TEXT NOT NULL CHECK (source IN
                                ('own_pipeline', 'peer_list', 'research', 'linkedin')),
                peer_history    TEXT,
                verify_flag     TEXT,
                gmail_draft_id  TEXT,
                removed_at      TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """INSERT INTO contacts_new
               SELECT id, name, company, email, linkedin_url, track, kind_detail,
                      priority, nyc_relevance, ask, status, date_contacted,
                      last_reply, next_step, notes, source, peer_history,
                      verify_flag, gmail_draft_id, NULL, created_at, updated_at
               FROM contacts"""
        )
        conn.execute("DROP TABLE contacts")
        conn.execute("ALTER TABLE contacts_new RENAME TO contacts")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_unique
            ON contacts (lower(email))
            WHERE email IS NOT NULL AND removed_at IS NULL
        """
    )
    conn.commit()


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_MATCH_WHERE = """
        WHERE j.gate_failed = 0 AND j.dismissed = 0 AND j.score IS NOT NULL
          AND c.ats_type != 'manual'
        """
_MATCH_SEARCH = r" AND (j.title LIKE ? ESCAPE '\' OR c.name LIKE ? ESCAPE '\')"


_MATCH_UNDECIDED = " AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)"


def fetch_matches(
    conn: sqlite3.Connection,
    query: str | None = None,
    limit: int | None = None,
    after: tuple[float, int] | None = None,
    undecided: bool = False,
) -> list[sqlite3.Row]:
    """Gate-passing, non-dismissed jobs, best score first.

    query, if given, is a case-insensitive substring filter over the job
    title and company name. limit caps the rows; after is the (score, id)
    of the last row already shown, so paging resumes by position in the
    ranking and a job dismissed meanwhile can't shift the next page.
    undecided leaves out jobs already tracked.
    """
    sql = """
        SELECT j.id, j.title, j.location, j.url, j.posted_at, j.score,
               j.score_breakdown, j.description, c.name AS company,
               -- Lane of the job's application, NULL if untracked (at most
               -- one application per job), so the feed pill can mirror the
               -- tracker board.
               (SELECT a.lane FROM applications a WHERE a.job_id = j.id)
                   AS tracked
        FROM jobs j JOIN companies c ON c.id = j.company_id
        """ + _MATCH_WHERE
    params: list = []
    if query:
        sql += _MATCH_SEARCH
        params += [_like_pattern(query)] * 2
    if undecided:
        sql += _MATCH_UNDECIDED
    if after is not None:
        sql += " AND (j.score < ? OR (j.score = ? AND j.id > ?))"
        params += [after[0], after[0], after[1]]
    sql += " ORDER BY j.score DESC, j.id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def fetch_job_detail(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """One job with what the feed report shows, whatever its feed status."""
    return conn.execute(
        """
        SELECT j.id, j.title, j.location, j.url, j.posted_at, j.score,
               j.score_breakdown, j.description, c.name AS company,
               (SELECT a.lane FROM applications a WHERE a.job_id = j.id)
                   AS tracked
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def count_matches(
    conn: sqlite3.Connection, query: str | None = None, undecided: bool = False
) -> int:
    """How many jobs fetch_matches would return without a limit."""
    sql = (
        "SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id = j.company_id"
        + _MATCH_WHERE
    )
    params: list[str] = []
    if query:
        sql += _MATCH_SEARCH
        params = [_like_pattern(query)] * 2
    if undecided:
        sql += _MATCH_UNDECIDED
    return conn.execute(sql, params).fetchone()[0]


def dismiss_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Mark a job dismissed. Returns False if no such job."""
    cur = conn.execute(
        "UPDATE jobs SET dismissed = 1, dismissed_at = datetime('now') WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def restore_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Un-dismiss a job so it reappears in the feed. Returns False if no such job."""
    cur = conn.execute(
        "UPDATE jobs SET dismissed = 0, dismissed_at = NULL WHERE id = ?", (job_id,)
    )
    conn.commit()
    return cur.rowcount > 0


def fetch_dismissed(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Dismissed jobs, most recently dismissed first.

    Rows dismissed before dismissed_at existed have no timestamp and
    sort last.
    """
    return conn.execute(
        """
        SELECT j.id, j.title, j.location, j.url, j.score, j.gate_failed,
               j.dismissed_at, c.name AS company
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE j.dismissed = 1
        ORDER BY j.dismissed_at IS NULL, j.dismissed_at DESC, j.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def fetch_rejected(
    conn: sqlite3.Connection, company: str | None = None
) -> list[sqlite3.Row]:
    """Gate-rejected jobs with their stored reasons, for auditing.

    company, if given, is a case-insensitive substring filter on the
    company name ("why don't I see any Stripe jobs?").
    """
    sql = """
        SELECT j.id, j.title, j.location, j.gate_reason, c.name AS company
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE j.gate_failed = 1
        """
    params: list[str] = []
    if company:
        sql += r" AND c.name LIKE ? ESCAPE '\'"
        params = [_like_pattern(company)]
    return conn.execute(sql + " ORDER BY c.name, j.title", params).fetchall()


def create_application(conn: sqlite3.Connection, job_id: int) -> int | None:
    """Track a job: new application in 'considering', plus a 'tracked' event.

    Returns the new application id, or None if the job is already tracked
    (one application per job). Raises sqlite3.IntegrityError for an
    unknown job_id (foreign key).
    """
    existing = conn.execute(
        "SELECT id FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()
    if existing is not None:
        return None
    cur = conn.execute("INSERT INTO applications (job_id) VALUES (?)", (job_id,))
    app_id = cur.lastrowid
    conn.execute(
        "INSERT INTO events (application_id, type, detail) VALUES (?, 'tracked', 'considering')",
        (app_id,),
    )
    conn.commit()
    return app_id


_APPLICATION_SELECT = """
    SELECT a.id, a.job_id, a.lane, a.resume_version, a.notes, a.next_action,
           a.next_action_due, a.applied_at, a.created_at, a.updated_at,
           j.title, j.url, j.location, j.score, j.score_breakdown,
           j.description, c.name AS company
    FROM applications a
    JOIN jobs j ON j.id = a.job_id
    JOIN companies c ON c.id = j.company_id
"""


def fetch_applications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All applications with their job/company, most recently touched first."""
    return conn.execute(
        _APPLICATION_SELECT + "ORDER BY a.updated_at DESC, a.id DESC"
    ).fetchall()


def get_application(conn: sqlite3.Connection, app_id: int) -> sqlite3.Row | None:
    return conn.execute(
        _APPLICATION_SELECT + "WHERE a.id = ?", (app_id,)
    ).fetchone()


def set_application_lane(conn: sqlite3.Connection, app_id: int, lane: str) -> bool:
    """Move an application to a lane, logging a 'lane_change' event.

    First move into 'applied' stamps applied_at. Returns False if no such
    application; a same-lane move is a no-op (no event).
    """
    row = conn.execute(
        "SELECT lane FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    if row is None:
        return False
    if lane == row["lane"]:
        return True
    conn.execute(
        """
        UPDATE applications
        SET lane = ?, updated_at = datetime('now'),
            -- localtime: applied_at is a human-facing date; a UTC stamp
            -- can show tomorrow's date for an evening move.
            applied_at = CASE WHEN ? = 'applied' AND applied_at IS NULL
                              THEN date('now', 'localtime') ELSE applied_at END
        WHERE id = ?
        """,
        (lane, lane, app_id),
    )
    conn.execute(
        "INSERT INTO events (application_id, type, detail) VALUES (?, 'lane_change', ?)",
        (app_id, f"{row['lane']} -> {lane}"),
    )
    conn.commit()
    return True


def find_job_by_source(
    conn: sqlite3.Connection, ats_type: str, slug: str, external_id: str
) -> sqlite3.Row | None:
    """The already-scraped job matching an ATS posting, if any."""
    return conn.execute(
        """
        SELECT j.id, j.title, j.gate_failed, j.gate_reason, c.name AS company,
               EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id) AS tracked
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE c.ats_type = ? AND c.slug = ? AND j.external_id = ?
        """,
        (ats_type, slug, external_id),
    ).fetchone()


def company_display_name(
    conn: sqlite3.Connection, ats_type: str, slug: str
) -> str | None:
    row = conn.execute(
        "SELECT name FROM companies WHERE ats_type = ? AND slug = ?", (ats_type, slug)
    ).fetchone()
    return row["name"] if row else None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "company"


def create_manual_job(
    conn: sqlite3.Connection,
    *,
    company: str,
    title: str,
    url: str | None,
    location: str | None,
    description: str | None,
    score: float | None = None,
    score_breakdown: str | None = None,
) -> int:
    """Insert a hand-entered job and track it (considering lane).

    The company is found-or-created under ats_type 'manual' with
    active=0 so the sweep never touches it. The caller supplies the
    score (db.py stays scoring-free); manual jobs are never gated and
    the matches feed excludes them regardless. Returns the new job id.
    """
    slug = _slugify(company)
    row = conn.execute(
        "SELECT id FROM companies WHERE ats_type = 'manual' AND slug = ?", (slug,)
    ).fetchone()
    if row is not None:
        company_id = row["id"]
    else:
        company_id = conn.execute(
            "INSERT INTO companies (name, ats_type, slug, active) VALUES (?, 'manual', ?, 0)",
            (company, slug),
        ).lastrowid
    job_id = conn.execute(
        """
        INSERT INTO jobs (company_id, external_id, title, url, location,
                          description, gate_failed, score, score_breakdown)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            company_id,
            f"manual-{uuid.uuid4().hex[:12]}",
            title,
            url,
            location,
            description,
            score,
            score_breakdown,
        ),
    ).lastrowid
    conn.commit()
    create_application(conn, job_id)
    return job_id


def untrack_application(conn: sqlite3.Connection, app_id: int) -> bool:
    """Delete a considering- or skipped-lane application and its events.

    Reverts the job to plain-match state (the feed's Track button comes
    back). Returns False if no such application. Raises ValueError once
    the card has progressed past those lanes — that history feeds
    analytics, so the card must be moved back first (deliberate two-step).
    """
    row = conn.execute(
        "SELECT lane FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    if row is None:
        return False
    if row["lane"] not in ("considering", "skipped"):
        raise ValueError(
            f"application {app_id} is in {row['lane']!r}, not considering/skipped"
        )
    conn.execute("DELETE FROM events WHERE application_id = ?", (app_id,))
    conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    conn.commit()
    return True


def update_application_details(
    conn: sqlite3.Connection,
    app_id: int,
    *,
    notes: str | None,
    resume_version: str | None,
    next_action: str | None,
    next_action_due: str | None,
    applied_at: str | None,
) -> bool:
    """Overwrite the editable card fields. Returns False if no such application."""
    cur = conn.execute(
        """
        UPDATE applications
        SET notes = ?, resume_version = ?, next_action = ?,
            next_action_due = ?, applied_at = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (notes, resume_version, next_action, next_action_due, applied_at, app_id),
    )
    conn.commit()
    return cur.rowcount > 0


def append_application_note(conn: sqlite3.Connection, app_id: int, text: str) -> bool:
    """Append a block of text to an application's notes field.

    For the MCP server: workflow output lands on the tracker card
    without clobbering hand-written notes (blank-line separator).
    Returns False if no such application.
    """
    row = conn.execute(
        "SELECT notes FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    if row is None:
        return False
    existing = (row["notes"] or "").rstrip()
    combined = f"{existing}\n\n{text}" if existing else text
    conn.execute(
        "UPDATE applications SET notes = ?, updated_at = datetime('now') WHERE id = ?",
        (combined, app_id),
    )
    conn.commit()
    return True


def fetch_analytics_applications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per application with the job fields analytics.py needs."""
    return conn.execute(
        """
        SELECT a.id, a.lane, a.resume_version, a.applied_at,
               j.score, j.first_seen, j.description
        FROM applications a JOIN jobs j ON j.id = a.job_id
        """
    ).fetchall()


def fetch_lane_changes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All lane_change events, oldest first, for deriving stages reached."""
    return conn.execute(
        "SELECT application_id, detail FROM events WHERE type = 'lane_change' ORDER BY id"
    ).fetchall()


def last_sweep_time(conn: sqlite3.Connection) -> str | None:
    """UTC timestamp of the most recent sweep touch, or None before any sweep.

    Every sweep bumps last_seen on each posting still listed, so the max
    across scraped jobs is when the boards were last read. Manual jobs
    are excluded — their last_seen is just their creation time.
    """
    row = conn.execute(
        """
        SELECT MAX(j.last_seen) AS stamp
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE c.ats_type != 'manual'
        """
    ).fetchone()
    return row["stamp"]


def add_email_suggestion(
    conn: sqlite3.Connection,
    *,
    application_id: int,
    msg_uid: str,
    from_addr: str | None,
    subject: str | None,
    received_at: str | None,
    suggested_lane: str | None,
    reason: str,
    body: str | None = None,
    message_id: str | None = None,
    from_name: str | None = None,
    kind: str | None = None,
    evidence: str | None = None,
    status: str = "pending",
) -> bool:
    """Store one inbox suggestion. Returns False if this message was
    already suggested for this application (any status). Automatic
    receipts arrive with status 'accepted': straight into reply history."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO email_suggestions
            (application_id, msg_uid, from_addr, subject, received_at,
             suggested_lane, reason, body, message_id, from_name, kind,
             evidence, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (application_id, msg_uid, from_addr, subject, received_at,
         suggested_lane, reason, body, message_id, from_name, kind,
         evidence, status),
    )
    conn.commit()
    return cur.rowcount > 0


def add_ignored_email(
    conn: sqlite3.Connection,
    *,
    msg_uid: str,
    company: str | None,
    from_name: str | None,
    from_addr: str | None,
    subject: str | None,
    received_at: str | None,
    message_id: str | None,
    reason: str,
) -> bool:
    """Record mail filtered out on purpose. False if already recorded."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO email_ignored
            (msg_uid, company, from_name, from_addr, subject, received_at,
             message_id, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (msg_uid, company, from_name, from_addr, subject, received_at,
         message_id, reason),
    )
    conn.commit()
    return cur.rowcount > 0


def fetch_ignored_emails(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
    """Most recent filtered mail, for the tracker's Ignored list."""
    return conn.execute(
        "SELECT * FROM email_ignored ORDER BY received_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def count_ignored_emails(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) AS n FROM email_ignored").fetchone()["n"]


def known_email_uids(conn: sqlite3.Connection) -> set[str]:
    """Every mail UID already handled, whichever application or list it
    went to. One email belongs to one place, so a known UID is never
    routed again when its application changes lanes."""
    rows = conn.execute(
        "SELECT msg_uid FROM email_suggestions UNION SELECT msg_uid FROM email_ignored"
    ).fetchall()
    return {row["msg_uid"] for row in rows}


def fetch_pending_suggestions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Pending suggestions, newest mail first, for the tracker cards."""
    return conn.execute(
        """
        SELECT id, application_id, msg_uid, from_addr, from_name, subject,
               received_at, suggested_lane, reason, body, message_id, kind,
               evidence
        FROM email_suggestions WHERE status = 'pending'
        ORDER BY received_at DESC, id DESC
        """
    ).fetchall()


def get_email_suggestion(conn: sqlite3.Connection, sid: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM email_suggestions WHERE id = ?", (sid,)
    ).fetchone()


def fetch_email_activity(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All non-dismissed suggestions, newest mail first.

    Pending rows are actionable on the tracker card; accepted (and
    lane-less pending) rows double as the application's reply history in
    the details section and modal. Dismissed rows were judged irrelevant
    (newsletters etc.) and stay hidden."""
    return conn.execute(
        """
        SELECT id, application_id, msg_uid, from_addr, from_name, subject,
               received_at, suggested_lane, reason, status, body, message_id,
               kind, evidence
        FROM email_suggestions WHERE status != 'dismissed'
        ORDER BY received_at DESC, id DESC
        """
    ).fetchall()


def set_suggestion_status(conn: sqlite3.Connection, sid: int, status: str) -> bool:
    """Resolve a suggestion (accepted/dismissed). Returns False if no such row."""
    cur = conn.execute(
        "UPDATE email_suggestions SET status = ? WHERE id = ?", (status, sid)
    )
    conn.commit()
    return cur.rowcount > 0


def record_sweep_run(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    companies: int,
    jobs_seen: int,
    jobs_new: int,
    errors: list[str],
    rescore_total: int | None = None,
    rescore_passed: int | None = None,
    rescore_rejected: int | None = None,
) -> int:
    """Log one completed sweep(+rescore). Returns the new row id."""
    cur = conn.execute(
        """
        INSERT INTO sweep_runs (started_at, companies, jobs_seen, jobs_new,
                                errors, rescore_total, rescore_passed, rescore_rejected)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at,
            companies,
            jobs_seen,
            jobs_new,
            json.dumps(errors) if errors else None,
            rescore_total,
            rescore_passed,
            rescore_rejected,
        ),
    )
    conn.commit()
    return cur.lastrowid


def last_sweep_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent recorded sweep run, or None (e.g. pre-feature DB)."""
    return conn.execute(
        "SELECT * FROM sweep_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def insert_contact(conn: sqlite3.Connection, **fields) -> int:
    """Insert a contact and return its id. Unknown fields are a bug: raises."""
    unknown = set(fields) - set(_CONTACT_FIELDS)
    if unknown:
        raise ValueError(f"unknown contact fields: {sorted(unknown)}")
    columns = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO contacts ({columns}) VALUES ({marks})",
        tuple(fields.values()),
    )
    conn.commit()
    return cur.lastrowid


def get_contact(conn: sqlite3.Connection, contact_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contacts WHERE id = ? AND removed_at IS NULL", (contact_id,)
    ).fetchone()


def fetch_contacts(
    conn: sqlite3.Connection, include_removed: bool = False
) -> list[sqlite3.Row]:
    """Every contact in the tool. include_removed is for the importer, which
    must recognize removed people so it doesn't add them back."""
    where = "" if include_removed else "WHERE removed_at IS NULL"
    return conn.execute(f"SELECT * FROM contacts {where} ORDER BY id").fetchall()


def remove_contact(conn: sqlite3.Connection, contact_id: int) -> bool:
    """Take a contact out of every list, count and tool. Reversible by
    clearing removed_at. Returns False if no such contact."""
    cur = conn.execute(
        "UPDATE contacts SET removed_at = datetime('now') "
        "WHERE id = ? AND removed_at IS NULL",
        (contact_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def update_contact(conn: sqlite3.Connection, contact_id: int, **fields) -> bool:
    """Write the given fields. Returns False if no such contact."""
    unknown = set(fields) - set(_CONTACT_FIELDS)
    if unknown:
        raise ValueError(f"unknown contact fields: {sorted(unknown)}")
    if not fields:
        return get_contact(conn, contact_id) is not None
    assignments = ", ".join(f"{name} = ?" for name in fields)
    cur = conn.execute(
        f"UPDATE contacts SET {assignments}, updated_at = datetime('now') "
        "WHERE id = ?",
        (*fields.values(), contact_id),
    )
    conn.commit()
    return cur.rowcount > 0


def add_interaction(
    conn: sqlite3.Connection,
    contact_id: int,
    date: str,
    channel: str,
    direction: str,
    summary: str,
) -> int:
    """Append to the interaction log. Status changes live in outreach.py."""
    cur = conn.execute(
        """
        INSERT INTO interactions (contact_id, date, channel, direction, summary)
        VALUES (?, ?, ?, ?, ?)
        """,
        (contact_id, date, channel, direction, summary),
    )
    conn.commit()
    return cur.lastrowid


def fetch_interactions(
    conn: sqlite3.Connection, contact_id: int | None = None
) -> list[sqlite3.Row]:
    """One contact's log, or every interaction when contact_id is None."""
    if contact_id is None:
        return conn.execute(
            "SELECT * FROM interactions ORDER BY date, id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM interactions WHERE contact_id = ? ORDER BY date, id",
        (contact_id,),
    ).fetchall()


def seen_contact_mail(conn: sqlite3.Connection) -> set[str]:
    """Message-IDs contact_sync has already handled."""
    rows = conn.execute("SELECT message_id FROM contact_mail").fetchall()
    return {row["message_id"] for row in rows}


def record_contact_mail(
    conn: sqlite3.Connection, message_id: str, contact_id: int | None, outcome: str
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO contact_mail (message_id, contact_id, outcome) VALUES (?, ?, ?)",
        (message_id, contact_id, outcome),
    )
    conn.commit()


def link_contact_application(
    conn: sqlite3.Connection,
    contact_id: int,
    application_id: int,
    note: str | None = None,
) -> None:
    """Tie a contact to a specific application. Re-linking updates the note."""
    conn.execute(
        """
        INSERT INTO contact_applications (contact_id, application_id, note)
        VALUES (?, ?, ?)
        ON CONFLICT (contact_id, application_id) DO UPDATE SET note = excluded.note
        """,
        (contact_id, application_id, note),
    )
    conn.commit()


def linked_contact_ids(conn: sqlite3.Connection, application_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT contact_id FROM contact_applications WHERE application_id = ?",
        (application_id,),
    ).fetchall()
    return {row["contact_id"] for row in rows}


def fetch_contact_links(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT contact_id, application_id FROM contact_applications"
    ).fetchall()


def fetch_companies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM companies ORDER BY id").fetchall()


def fetch_open_jobs(
    conn: sqlite3.Connection, company_ids: list[int], contact_id: int
) -> list[sqlite3.Row]:
    """Jobs at these companies that a referral could still be tied to, newest first.

    gate_failed = 0 is the feed's own test, so the user's locations, excluded
    titles and delisted postings all apply. Dismissed jobs and jobs already
    linked to this contact are left out.
    """
    if not company_ids:
        return []
    marks = ", ".join("?" for _ in company_ids)
    return conn.execute(
        f"""
        SELECT j.id, j.title, j.location, j.url, j.description, j.score,
               j.first_seen, c.name AS company
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE j.company_id IN ({marks}) AND j.gate_failed = 0 AND j.dismissed = 0
          AND NOT EXISTS (
              SELECT 1 FROM applications a
              JOIN contact_applications ca ON ca.application_id = a.id
              WHERE a.job_id = j.id AND ca.contact_id = ?
          )
        ORDER BY j.first_seen DESC, j.id
        """,
        (*company_ids, contact_id),
    ).fetchall()


# --- daily schedule ---------------------------------------------------------


def log_block(
    conn: sqlite3.Connection, day: str, block_key: str, note: str | None, source: str
) -> None:
    """Mark a block done for a day. A second mark keeps the row and appends the note."""
    conn.execute(
        """
        INSERT INTO day_log (day, block_key, note, source) VALUES (?, ?, ?, ?)
        ON CONFLICT(day, block_key) DO UPDATE SET
            note = CASE
                WHEN excluded.note IS NULL OR excluded.note = '' THEN day_log.note
                WHEN day_log.note IS NULL OR day_log.note = '' THEN excluded.note
                ELSE day_log.note || char(10) || excluded.note
            END,
            source = excluded.source
        """,
        (day, block_key, note or None, source),
    )
    conn.commit()


def clear_block(conn: sqlite3.Connection, day: str, block_key: str) -> bool:
    cur = conn.execute(
        "DELETE FROM day_log WHERE day = ? AND block_key = ?", (day, block_key)
    )
    conn.commit()
    return cur.rowcount > 0


def fetch_day_logs(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """Check-offs for days start..end inclusive (ISO dates)."""
    return conn.execute(
        "SELECT day, block_key, note, source FROM day_log"
        " WHERE day BETWEEN ? AND ? ORDER BY day, id",
        (start, end),
    ).fetchall()


def fetch_schedule_activity(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """What the tracker and contacts saw happen on each day in start..end.

    Timestamps stored in UTC (events, jobs.dismissed_at, sweep_runs) are read
    in local time so a 9 PM lane move lands on the right day. interactions.date
    is already a local date. Every row carries its day so schedule.py can
    bucket them without touching the database.
    """
    lane_moves = conn.execute(
        """
        SELECT date(e.occurred_at, 'localtime') AS day, e.application_id, e.detail,
               time(e.occurred_at, 'localtime') AS at, j.title, c.name AS company
        FROM events e
        JOIN applications a ON a.id = e.application_id
        JOIN jobs j ON j.id = a.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE e.type IN ('lane_change', 'tracked')
          AND date(e.occurred_at, 'localtime') BETWEEN ? AND ?
        ORDER BY e.id
        """,
        (start, end),
    ).fetchall()
    dismissed = conn.execute(
        """
        SELECT date(dismissed_at, 'localtime') AS day, COUNT(*) AS n
        FROM jobs WHERE dismissed = 1 AND dismissed_at IS NOT NULL
          AND date(dismissed_at, 'localtime') BETWEEN ? AND ?
        GROUP BY 1
        """,
        (start, end),
    ).fetchall()
    interactions = conn.execute(
        """
        SELECT i.date AS day, i.direction, i.channel, i.summary, i.contact_id,
               c.name, c.company, c.track,
               (c.date_contacted = i.date) AS first_touch
        FROM interactions i JOIN contacts c ON c.id = i.contact_id
        WHERE i.date BETWEEN ? AND ? AND c.removed_at IS NULL
        ORDER BY i.date, i.id
        """,
        (start, end),
    ).fetchall()
    sweeps = conn.execute(
        """
        SELECT date(finished_at, 'localtime') AS day, time(finished_at, 'localtime') AS at
        FROM sweep_runs WHERE date(finished_at, 'localtime') BETWEEN ? AND ?
        ORDER BY id
        """,
        (start, end),
    ).fetchall()
    return {
        "lane_moves": [dict(r) for r in lane_moves],
        "dismissed": {r["day"]: r["n"] for r in dismissed},
        "interactions": [dict(r) for r in interactions],
        "sweeps": [dict(r) for r in sweeps],
    }


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(row["name"] for row in rows)
