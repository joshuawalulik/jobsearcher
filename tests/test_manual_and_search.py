"""Feed search + manually-added jobs.

Same pattern as test_tracker.py: TestClient with the get_db dependency
overridden against a seeded tmp database.
"""

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import db
from app import app, get_db
from scoring.rescore import rescore_all


def seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (2, 'Bravo', 'lever', 'bravo')"
    )
    ids = {}
    for key, company_id, title in [
        ("payments", 1, "Software Engineer, Payments"),
        ("platform", 1, "Platform Engineer"),
        ("bravo", 2, "Fullstack Engineer 100% Remote"),
    ]:
        cur = conn.execute(
            """INSERT INTO jobs (company_id, external_id, title, location, url,
               score, gate_failed, dismissed)
               VALUES (?, ?, ?, 'New York, NY', 'https://example.com/job', 42.5, 0, 0)""",
            (company_id, key, title),
        )
        ids[key] = cur.lastrowid
    conn.commit()
    return ids


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test.db"
    conn = db.get_connection(db_path)
    db.init_db(conn)
    client = TestClient(app)
    client.job_ids = seed(conn)
    client.db = conn

    def override():
        request_conn = db.get_connection(db_path, check_same_thread=False)
        try:
            yield request_conn
        finally:
            request_conn.close()

    app.dependency_overrides[get_db] = override
    yield client
    app.dependency_overrides.clear()
    conn.close()


# --- search ------------------------------------------------------------------

def test_search_filters_by_title_case_insensitive(client):
    page = client.get("/", params={"q": "payments"}).text
    assert "Software Engineer, Payments" in page
    assert "Platform Engineer" not in page


def test_search_filters_by_company(client):
    page = client.get("/feed", params={"q": "bravo"}).text
    assert "Fullstack Engineer" in page
    assert "Software Engineer, Payments" not in page


def test_search_empty_query_returns_all(client):
    page = client.get("/feed", params={"q": ""}).text
    assert ">3</span>" in page


def test_search_escapes_like_wildcards(client):
    # '%' must match literally, not as a LIKE wildcard.
    page = client.get("/feed", params={"q": "100%"}).text
    assert "Fullstack Engineer" in page
    assert ">1</span>" in page
    assert ">0</span>" in client.get("/feed", params={"q": "%%%"}).text


# --- manual jobs ---------------------------------------------------------------

def add_manual(client, **overrides):
    form = {
        "company": "Startup Co",
        "title": "Founding Engineer",
        "url": "startup.example/careers/1",
        "location": "New York, NY",
        "description": "Ship product end to end.",
    }
    form.update(overrides)
    return client.post("/jobs/manual", data=form, follow_redirects=False)


def test_manual_job_lands_in_considering(client):
    response = add_manual(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/tracker"
    job = client.db.execute(
        "SELECT j.*, c.name, c.ats_type, c.active FROM jobs j "
        "JOIN companies c ON c.id = j.company_id WHERE j.title = 'Founding Engineer'"
    ).fetchone()
    assert job["ats_type"] == "manual"
    assert job["active"] == 0  # sweep never touches manual companies
    assert job["score"] is not None  # scored at creation, no gates
    assert job["score_breakdown"] is not None
    assert job["url"] == "https://startup.example/careers/1"  # scheme added
    application = client.db.execute(
        "SELECT lane FROM applications WHERE job_id = ?", (job["id"],)
    ).fetchone()
    assert application["lane"] == "considering"
    page = client.get("/tracker").text
    assert "Founding Engineer" in page and "Startup Co" in page


def test_manual_job_stays_out_of_matches_feed(client):
    add_manual(client)
    assert "Founding Engineer" not in client.get("/").text


def test_manual_company_reused_on_second_add(client):
    add_manual(client)
    add_manual(client, title="Second Role")
    n = client.db.execute(
        "SELECT count(*) AS n FROM companies WHERE ats_type = 'manual'"
    ).fetchone()["n"]
    assert n == 1


def test_manual_job_requires_company_and_title(client):
    assert add_manual(client, company="  ").status_code == 422
    assert add_manual(client, title="").status_code == 422


def test_rescore_scores_manual_jobs_without_gating(client):
    add_manual(client)
    profile = {
        "gates": {
            "excluded_title_keywords": ["founding"],  # would reject it if gated
            "work_type": {"mine": "hybrid", "reject_conflicts": False},
            "allowed_locations": [],
            "clearance_keywords": [],
            "max_posting_age_days": 30,
        },
        "scoring": {
            "target_titles": ["Software Engineer"],
            "title_match_weight": 30, "exact_title_bonus": 15,
            "required_keywords": [], "required_keyword_weight": 5,
            "preferred_keywords": [], "preferred_keyword_weight": 2,
            "negative_keywords": [], "negative_keyword_weight": -10,
            "must_haves": [], "missing_must_have_penalty": -20,
            "seniority": {"target": "mid", "multipliers": {"mid": 1.0}},
            "sparse_description": {"min_chars": 400, "multiplier": 0.85},
        },
    }
    summary = rescore_all(client.db, profile, now=datetime.now(timezone.utc))
    assert summary["total"] == 4  # three scraped + the manual one
    row = client.db.execute(
        "SELECT score, gate_failed FROM jobs WHERE title = 'Founding Engineer'"
    ).fetchone()
    # 'founding' is an excluded title keyword, but manual jobs skip gates
    # and still get a fresh score.
    assert row["gate_failed"] == 0
    assert row["score"] is not None


# --- companies CHECK migration -------------------------------------------------

def test_init_db_rebuilds_companies_check_for_manual(tmp_path):
    conn = db.get_connection(tmp_path / "old.db")
    conn.executescript(
        """
        CREATE TABLE companies (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            ats_type    TEXT NOT NULL CHECK (ats_type IN ('greenhouse', 'lever', 'ashby')),
            slug        TEXT NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (ats_type, slug)
        );
        INSERT INTO companies (id, name, ats_type, slug) VALUES (7, 'Acme', 'greenhouse', 'acme');
        """
    )
    db.init_db(conn)
    # Existing rows and ids survive the rebuild...
    row = conn.execute("SELECT id, name FROM companies WHERE slug = 'acme'").fetchone()
    assert (row["id"], row["name"]) == (7, "Acme")
    # ...and 'manual' is now allowed where it previously violated the CHECK.
    conn.execute("INSERT INTO companies (name, ats_type, slug) VALUES ('X', 'manual', 'x')")
    db.init_db(conn)  # idempotent second run
    conn.close()


def test_rescore_keeps_scores_for_tracked_gate_failed_jobs(client):
    # Track 'payments', then use a profile whose gates reject both
    # 'payments' (tracked) and 'platform' (untracked).
    client.post(f"/jobs/{client.job_ids['payments']}/track")
    profile = {
        "gates": {
            "excluded_title_keywords": ["payments", "platform"],
            "work_type": {"mine": "hybrid", "reject_conflicts": False},
            "allowed_locations": [],
            "clearance_keywords": [],
            "max_posting_age_days": 30,
        },
        "scoring": {
            "target_titles": ["Software Engineer"],
            "title_match_weight": 30, "exact_title_bonus": 15,
            "required_keywords": [], "required_keyword_weight": 5,
            "preferred_keywords": [], "preferred_keyword_weight": 2,
            "negative_keywords": [], "negative_keyword_weight": -10,
            "must_haves": [], "missing_must_have_penalty": -20,
            "seniority": {"target": "mid", "multipliers": {"mid": 1.0}},
            "sparse_description": {"min_chars": 400, "multiplier": 0.85},
        },
    }
    rescore_all(client.db, profile, now=datetime.now(timezone.utc))
    tracked = client.db.execute(
        "SELECT score, gate_failed FROM jobs WHERE id = ?",
        (client.job_ids["payments"],),
    ).fetchone()
    assert tracked["gate_failed"] == 1
    assert tracked["score"] is not None  # tracked: score survives rejection
    untracked = client.db.execute(
        "SELECT score, gate_failed FROM jobs WHERE id = ?",
        (client.job_ids["platform"],),
    ).fetchone()
    assert untracked["gate_failed"] == 1
    assert untracked["score"] is None  # untracked: nulled as before
