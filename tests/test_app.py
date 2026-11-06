"""Route tests for the Phase 3 UI, via TestClient over a tmp SQLite DB.

The get_db dependency is overridden to open per-request connections to
a seeded tmp database; profile-touching routes get a tmp copy of the
real profile.yaml via monkeypatching app.PROFILE_PATH.
"""

import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db
import profile_io
from app import app, get_db

REAL_PROFILE = Path(__file__).parent.parent / "config" / "profile.yaml"

BREAKDOWN = {
    "score": 42.5,
    "subtotal": 50,
    "seniority": "mid",
    "components": [
        {"reason": "title matches target 'Software Engineer'", "points": 30},
        {"reason": "required keyword 'react'", "points": 5},
        {"reason": "negative keyword 'php'", "points": -10},
    ],
    "multipliers": [{"reason": "sparse description x0.85", "factor": 0.85}],
}


def seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    jobs = {
        "match": (
            "Software Engineer, Payments",
            "New York, NY",
            42.5,
            json.dumps(BREAKDOWN),
            0,
            None,
            0,
        ),
        "dismissed": ("Fullstack Engineer DISMISSED-ROW", "New York, NY", 60.0, "{}", 0, None, 1),
        "rejected": (
            "Engineering Manager REJECTED-ROW",
            "San Francisco",
            None,
            None,
            1,
            "excluded-title: title contains 'manager'; "
            "location: 'San Francisco' matches none of allowed_locations",
            0,
        ),
        # A location containing "; " must not split into a bogus gate group.
        "rejected_semicolon": (
            "Backend Engineer SEMICOLON-ROW",
            "Remote - EU; Dublin, Ireland",
            None,
            None,
            1,
            "location: 'Remote - EU; Dublin, Ireland' matches none of allowed_locations",
            0,
        ),
    }
    ids = {}
    for key, (title, location, score, breakdown, failed, reason, dismissed) in jobs.items():
        cur = conn.execute(
            """INSERT INTO jobs (company_id, external_id, title, location, description,
               url, score, score_breakdown, gate_failed, gate_reason, dismissed)
               VALUES (1, ?, ?, ?, 'react and typescript all day', 'https://example.com/job',
                       ?, ?, ?, ?, ?)""",
            (key, title, location, score, breakdown, failed, reason, dismissed),
        )
        ids[key] = cur.lastrowid
    conn.commit()
    return ids


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    profile_path = tmp_path / "profile.yaml"
    shutil.copy(REAL_PROFILE, profile_path)
    monkeypatch.setattr(app_module, "PROFILE_PATH", profile_path)
    yield client
    app.dependency_overrides.clear()
    conn.close()


def test_feed_shows_matches_with_breakdown(client):
    page = client.get("/").text
    assert "Software Engineer, Payments" in page
    assert "DISMISSED-ROW" not in page
    assert "REJECTED-ROW" not in page
    assert "42.5" in page
    # Rows stay light: the full reasons load into the report on click.
    assert "sparse description x0.85" not in page
    job_id = int(re.search(r'id="job-(\d+)"', page).group(1))
    report = client.get(f"/jobs/{job_id}/detail").text
    assert "Why ranked here" in report
    assert "title matches target &#39;Software Engineer&#39;" in report
    assert "+5" in report and "-10" in report
    assert "sparse description x0.85" in report
    assert "42.5" in report


def test_job_detail_unknown_job_404s(client):
    assert client.get("/jobs/999999/detail").status_code == 404


def test_score_strip_buckets_points_on_one_ramp():
    strip = app_module.score_strip({"components": [
        {"category": "title", "points": 20},
        {"category": "required", "points": 30},
        {"category": "preferred", "points": 10},
        {"category": "negative", "points": -5},
        {"category": "experience", "points": -3},
    ]})
    assert (strip["title"], strip["required"], strip["preferred"], strip["penalty"]) == (20, 30, 10, 8)
    assert strip["widths"]["required"] == 30  # 1 point is 1% of the track


def test_score_strip_never_overflows_the_track():
    strip = app_module.score_strip({"components": [
        {"category": "required", "points": 150},
        {"category": "title", "points": 50},
    ]})
    assert sum(strip["widths"].values()) == 100


def test_score_strip_skips_breakdowns_without_categories():
    assert app_module.score_strip(BREAKDOWN) is None
    assert app_module.score_strip(None) is None


@pytest.mark.parametrize("hour, minute, expected", [
    (7, 0, "8:30"), (8, 30, "16:30"), (12, 0, "16:30"), (18, 40, "8:30"),
])
def test_next_sweep_label(hour, minute, expected):
    now = datetime(2026, 11, 5, hour, minute)
    assert app_module.next_sweep_label(now) == expected


def test_dismiss_removes_job(client):
    job_id = client.job_ids["match"]
    assert client.post(f"/jobs/{job_id}/dismiss").status_code == 200
    row = client.db.execute("SELECT dismissed FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["dismissed"] == 1
    assert "Software Engineer, Payments" not in client.get("/").text


def test_dismiss_unknown_job_404s(client):
    assert client.post("/jobs/999999/dismiss").status_code == 404


def test_rescore_reruns_scoring_and_returns_feed(client):
    page = client.post("/rescore").text
    assert "Rescored 4 jobs" in page
    # Real gates now judge the fixtures: the manager posting stays rejected,
    # the NYC engineer stays in the feed with a freshly computed breakdown.
    assert "Software Engineer, Payments" in page
    assert "REJECTED-ROW" not in page
    row = client.db.execute(
        "SELECT gate_reason FROM jobs WHERE id = ?", (client.job_ids["rejected"],)
    ).fetchone()
    assert "excluded-title" in row["gate_reason"]


def test_rejected_view_groups_by_gate(client):
    page = client.get("/rejected").text
    assert "excluded-title" in page
    assert "location" in page
    assert "REJECTED-ROW" in page
    assert "title contains &#39;manager&#39;" in page
    # The "; " inside the location stays one reason under the location gate.
    assert "SEMICOLON-ROW" in page
    assert "<strong>Dublin" not in page


def test_profile_editor_renders_current_values(client):
    page = client.get("/profile").text
    assert "max_posting_age_days" in page
    assert "recruiter" in page  # excluded title keyword in textarea
    assert "FanDuel | greenhouse | fanduel" in page  # watchlist textarea
    assert "Save &amp; re-score" in page


def _form_from_profile(path: Path) -> dict[str, str]:
    doc = profile_io.load_round_trip(path)
    form = {}
    for field, kind in profile_io.FIELDS.items():
        value = profile_io.get_value(doc, field)
        if kind == "list":
            form[field] = "\n".join(value)
        elif kind == "companies":
            form[field] = "\n".join(
                f"{e['name']} | {e['ats']} | {e['slug']}" for e in value
            )
        elif kind == "bool":
            if value:
                form[field] = "on"
        else:
            form[field] = str(value)
    return form


def test_profile_save_writes_yaml_and_rescores(client, tmp_path):
    profile_path = tmp_path / "profile.yaml"
    form = _form_from_profile(profile_path)
    form["gates.max_posting_age_days"] = "45"

    page = client.post("/profile", data=form).text
    assert "Saved." in page
    assert "Rescored 4 jobs" in page
    text = profile_path.read_text()
    assert "max_posting_age_days: 45" in text
    assert "# US-remote only" in text  # comments survive the save


def test_profile_save_rejects_bad_input(client, tmp_path):
    profile_path = tmp_path / "profile.yaml"
    original = profile_path.read_text()
    form = _form_from_profile(profile_path)
    form["scoring.title_match_weight"] = "not-a-number"

    response = client.post("/profile", data=form)
    assert response.status_code == 422
    assert "not a number" in response.text
    assert profile_path.read_text() == original  # nothing written


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "jobs" in body["tables"]


def test_rejected_company_filter(client):
    client.db.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (2, 'Beta Corp', 'lever', 'beta')"
    )
    client.db.execute(
        """INSERT INTO jobs (company_id, external_id, title, location, gate_failed,
           gate_reason, dismissed)
           VALUES (2, 'b1', 'Staff Engineer BETA-ROW', 'Austin, TX', 1,
                   "excluded-title: title contains 'staff'", 0)"""
    )
    client.db.commit()
    # Full page with ?q= filters, case-insensitively.
    page = client.get("/rejected", params={"q": "beta"}).text
    assert "BETA-ROW" in page
    assert "REJECTED-ROW" not in page
    # HTMX partial does the same and auto-opens groups.
    partial = client.get("/rejected/groups", params={"q": "BETA"}).text
    assert "BETA-ROW" in partial and "<details" in partial and "open" in partial
    # Unfiltered still shows both companies.
    both = client.get("/rejected").text
    assert "BETA-ROW" in both and "REJECTED-ROW" in both


def test_rejected_filter_no_hits_message(client):
    page = client.get("/rejected/groups", params={"q": "nonesuch"}).text
    assert "No rejections from companies matching" in page


def test_feed_shows_last_sweep_stamp(client):
    page = client.get("/").text
    assert "Swept " in page
    assert "min ago" in page  # seeded last_seen is datetime('now')
    # No recorded runs yet (pre-feature DB): stamp works without counts.
    assert "passed gates" not in page


def test_no_sweep_stamp_on_empty_db(client):
    client.db.execute("DELETE FROM jobs")
    client.db.commit()
    assert "Boards last swept" not in client.get("/").text


def test_sweep_button_sweeps_rescores_and_reports(client, monkeypatch):
    from scrapers import sweep as sweep_module

    monkeypatch.setattr(
        sweep_module, "run",
        lambda conn, profile: {
            "companies": 2, "seen": 7, "new": 3,
            "errors": ["Beta: board not found — check slug 'beta'"],
        },
    )
    page = client.post("/sweep").text
    assert "Swept 2 companies: 7 jobs (3 new), 1 error" in page
    assert "Rescored 4 jobs" in page  # the rescore ran for real over the seed
    # The out-of-band stamp update carries the run summary and its errors.
    assert 'hx-swap-oob="true"' in page
    assert "3 new from 2 companies" in page
    assert "board not found" in page

    # The run was recorded and shows on a fresh page load too.
    home = client.get("/").text
    assert "3 new from 2 companies" in home
    assert "1 sweep error" in home


def test_feed_has_sweep_button(client):
    page = client.get("/").text
    assert 'hx-post="/sweep"' in page


def test_short_date_shows_the_year_only_when_it_differs():
    this_year = datetime.now().year
    assert app_module.short_date(f"{this_year}-11-04 10:00:00") == "Nov 4"
    assert app_module.short_date(f"{this_year - 1}-01-28") == f"Jan 28, {this_year - 1}"
    assert app_module.short_date(None) == ""
