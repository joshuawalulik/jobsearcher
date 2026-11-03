"""Phase 5 analytics: pure-function tests plus /analytics route tests.

Route tests follow test_tracker.py: TestClient with the get_db dependency
overridden against a seeded tmp database, driving real endpoints so the
events history is the genuine article.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import analytics
import db
from app import app, get_db


def make_app(app_id, lane="considering", applied_at=None, score=50.0,
             resume_version=None, first_seen="2026-07-01 12:00:00", description=""):
    return {
        "id": app_id,
        "lane": lane,
        "applied_at": applied_at,
        "score": score,
        "resume_version": resume_version,
        "first_seen": first_seen,
        "description": description,
    }


def change(app_id, old, new):
    return {"application_id": app_id, "detail": f"{old} -> {new}"}


class TestComputeReached:
    def test_untouched_app_reached_considering_only(self):
        reached = analytics.compute_reached([make_app(1)], [])
        assert reached == {1: analytics._RANK["considering"]}

    def test_bounce_keeps_furthest_stage(self):
        # now rejected, but progress is the max ever reached
        apps = [make_app(1, lane="rejected", applied_at="2026-07-02")]
        events = [
            change(1, "considering", "applied"),
            change(1, "applied", "interviewing"),
            change(1, "interviewing", "rejected"),
        ]
        reached = analytics.compute_reached(apps, events)
        assert reached[1] == analytics.ADVANCED

    def test_retired_lane_events_carry_no_progress(self):
        # History from the removed screening/ghosted lanes ranks as nothing.
        apps = [make_app(1, lane="rejected", applied_at="2026-07-02")]
        events = [change(1, "applied", "screening"), change(1, "screening", "ghosted")]
        reached = analytics.compute_reached(apps, events)
        assert reached[1] == analytics.APPLIED  # from applied_at, not the events

    def test_terminal_lane_without_history_counts_as_considering(self):
        reached = analytics.compute_reached([make_app(1, lane="rejected")], [])
        assert reached[1] == analytics._RANK["considering"]

    def test_manual_applied_at_counts_as_applied(self):
        apps = [make_app(1, lane="considering", applied_at="2026-07-02")]
        assert analytics.compute_reached(apps, [])[1] == analytics.APPLIED

    def test_skipping_a_lane_still_counts_lower_stages(self):
        # considering -> interviewing never sat in applied, but an interview
        # implies an application: reached >= applied holds by rank order.
        apps = [make_app(1, lane="interviewing")]
        events = [change(1, "considering", "interviewing")]
        reached = analytics.compute_reached(apps, events)
        assert reached[1] >= analytics.APPLIED


class TestBands:
    def test_boundaries(self):
        apps = [
            make_app(1, lane="applied", score=39.9),
            make_app(2, lane="applied", score=40.0),
            make_app(3, lane="interviewing", score=50.0),
        ]
        reached = analytics.compute_reached(apps, [])
        rows = analytics.reply_rate_by_band(apps, reached, [40, 50])
        assert [(r["label"], r["applied"], r["advanced"]) for r in rows] == [
            ("< 40", 1, 0),
            ("40–50", 1, 0),
            ("50+", 1, 1),
        ]
        assert rows[2]["rate"] == 100

    def test_empty_band_has_no_rate(self):
        rows = analytics.reply_rate_by_band([], {}, [40, 50])
        assert all(r["applied"] == 0 and r["rate"] is None for r in rows)

    def test_unscored_applied_job_gets_own_row(self):
        apps = [make_app(1, lane="applied", score=None)]
        reached = analytics.compute_reached(apps, [])
        rows = analytics.reply_rate_by_band(apps, reached, [40, 50])
        assert rows[-1]["label"] == "no score"
        assert rows[-1]["applied"] == 1

    def test_considering_only_apps_are_excluded(self):
        apps = [make_app(1, lane="considering", score=90.0)]
        reached = analytics.compute_reached(apps, [])
        rows = analytics.reply_rate_by_band(apps, reached, [40, 50])
        assert all(r["applied"] == 0 for r in rows)


class TestResumes:
    def test_groups_and_outcomes(self):
        apps = [
            make_app(1, lane="offer", resume_version="v3"),
            make_app(2, lane="rejected", applied_at="2026-07-02", resume_version="v3"),
            make_app(3, lane="applied", resume_version=None),
        ]
        events = [
            change(1, "considering", "applied"),
            change(1, "applied", "offer"),
        ]
        reached = analytics.compute_reached(apps, events)
        rows = analytics.outcomes_by_resume(apps, reached)
        v3 = next(r for r in rows if r["label"] == "v3")
        assert (v3["applied"], v3["advanced"], v3["offers"], v3["dead"]) == (2, 1, 1, 1)
        assert next(r for r in rows if r["label"] == "(not set)")["applied"] == 1


class TestSpeed:
    def test_fast_vs_slow_split(self):
        apps = [
            make_app(1, lane="interviewing", applied_at="2026-07-02",
                     first_seen="2026-07-01 22:00:00"),
            make_app(2, lane="applied", applied_at="2026-07-10",
                     first_seen="2026-07-01 08:00:00"),
        ]
        reached = analytics.compute_reached(apps, [])
        result = analytics.speed_effect(apps, reached, 48)
        fast, slow = result["rows"]
        assert (fast["applied"], fast["advanced"]) == (1, 1)
        assert (slow["applied"], slow["advanced"]) == (1, 0)
        assert result["unknown"] == 0

    def test_missing_applied_at_counts_as_unknown(self):
        # reached applied via events, but no date to compare
        apps = [make_app(1, lane="applied", applied_at=None)]
        events = [change(1, "considering", "applied")]
        reached = analytics.compute_reached(apps, events)
        result = analytics.speed_effect(apps, reached, 48)
        assert result["unknown"] == 1
        assert all(r["applied"] == 0 for r in result["rows"])


class TestKeywords:
    def test_hits_split_by_outcome_and_zero_rows_dropped(self):
        apps = [
            make_app(1, lane="interviewing", description="React and TypeScript stack"),
            make_app(2, lane="applied", applied_at="2026-07-02",
                     description="TypeScript maintainer"),
            make_app(3, lane="applied", applied_at="2026-07-02", description=None),
        ]
        events = [change(1, "considering", "applied"), change(1, "applied", "interviewing")]
        reached = analytics.compute_reached(apps, events)
        result = analytics.keywords_that_advance(
            apps, reached, ["react", "typescript", "kafka", "ai"]
        )
        assert result["advanced_total"] == 1
        assert result["stalled_total"] == 2
        rows = {r["keyword"]: (r["advanced"], r["stalled"]) for r in result["rows"]}
        # kafka hit nothing -> dropped; "ai" must not match "maintainer"
        assert rows == {"react": (1, 0), "typescript": (1, 1)}


# --- route tests -----------------------------------------------------------


def seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    ids = {}
    rows = [
        ("high", "Software Engineer", 62.0, "React, TypeScript and node services"),
        ("mid", "Full Stack Engineer", 45.0, "JavaScript and css work"),
        ("low", "Web Engineer", 30.0, None),
    ]
    for key, title, score, description in rows:
        cur = conn.execute(
            """INSERT INTO jobs (company_id, external_id, title, location, url,
               description, score, gate_failed, dismissed, first_seen)
               VALUES (1, ?, ?, 'New York, NY', 'https://example.com/job', ?, ?, 0, 0,
                       '2026-07-01 12:00:00')""",
            (key, title, description, score),
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


def track_to(client, key, lanes=()):
    """Track a seeded job and walk it through lanes via the real endpoints."""
    assert client.post(f"/jobs/{client.job_ids[key]}/track").status_code == 200
    app_id = client.db.execute(
        "SELECT id FROM applications WHERE job_id = ?", (client.job_ids[key],)
    ).fetchone()["id"]
    for lane in lanes:
        assert client.post(f"/applications/{app_id}/lane", data={"lane": lane}).status_code == 200
    return app_id


def test_empty_db_renders_zeros(client):
    page = client.get("/analytics")
    assert page.status_code == 200
    assert "0 tracked applications" in page.text
    assert "No applications yet." in page.text
    assert "No keyword hits" in page.text


def test_page_reflects_real_event_history(client):
    # high: applied -> interviewing -> rejected (advanced, now dead)
    app_id = track_to(client, "high", ["applied", "interviewing", "rejected"])
    client.post(f"/applications/{app_id}", data={
        "notes": "", "resume_version": "fullstack-v3", "next_action": "",
        "next_action_due": "", "applied_at": "2026-07-02",
    })
    # mid: applied, no reply yet
    track_to(client, "mid", ["applied"])
    # low: still considering — excluded from applied population
    track_to(client, "low")

    page = client.get("/analytics").text
    assert "3 tracked applications" in page
    assert "fullstack-v3" in page
    # keyword table: react appears in the advanced job's description
    assert "react" in page
    # funnel conversions: 2 ever applied, 1 ever interviewed
    assert "applied <strong>2</strong>" in page
    assert "interviewing <strong>1</strong>" in page


def test_score_bands_use_profile_edges(client):
    track_to(client, "high", ["applied", "interviewing"]) # 62 -> 50+, advanced
    track_to(client, "mid", ["applied"])                 # 45 -> 40–50, stalled
    page = client.get("/analytics").text
    assert "50+" in page and "40–50" in page and "&lt; 40" in page
    assert "1/1 (100%)" in page  # 50+ band advanced
