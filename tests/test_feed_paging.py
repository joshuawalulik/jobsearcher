"""Feed paging: every job appears exactly once, whatever happens between pages."""

import json
import re

import pytest
from fastapi.testclient import TestClient

import db
from app import FEED_PAGE, app, get_db


def seed_jobs(conn, count, score=lambda i: 90.0):
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    ids = [
        conn.execute(
            """INSERT INTO jobs (company_id, external_id, title, score, gate_failed, dismissed)
               VALUES (1, ?, ?, ?, 0, 0)""",
            (f"x{i}", "Python role" if i % 2 else "Other role", score(i)),
        ).lastrowid
        for i in range(count)
    ]
    conn.commit()
    return ids


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(conn, tmp_path):
    def override():
        request_conn = db.get_connection(tmp_path / "test.db")
        try:
            yield request_conn
        finally:
            request_conn.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def walk(conn, page, query=None):
    seen, after = [], None
    while True:
        rows = db.fetch_matches(conn, query, limit=page, after=after)
        if not rows:
            return seen
        seen += [r["id"] for r in rows]
        after = (rows[-1]["score"], rows[-1]["id"])


def test_tied_scores_never_skip_or_repeat(conn):
    ids = seed_jobs(conn, 25, score=lambda i: 50.0 + i % 3)
    assert sorted(walk(conn, 7)) == sorted(ids)
    assert walk(conn, 7) == [r["id"] for r in db.fetch_matches(conn)]


def test_dismissing_between_pages_keeps_the_next_job(conn):
    ids = seed_jobs(conn, 10)
    first = db.fetch_matches(conn, limit=5)
    db.dismiss_job(conn, first[0]["id"])
    rest = db.fetch_matches(conn, limit=5, after=(first[-1]["score"], first[-1]["id"]))
    assert [r["id"] for r in rest] == ids[5:]


def test_search_pages_and_counts_across_all_jobs(conn):
    seed_jobs(conn, 20)
    assert len(walk(conn, 3, "python")) == 10
    assert db.count_matches(conn, "python") == 10
    assert db.count_matches(conn) == 20


def test_page_shows_the_total_and_only_one_page_of_jobs(conn, client):
    seed_jobs(conn, FEED_PAGE + 3)
    page = client.get("/").text
    assert f">{FEED_PAGE + 3}</span>" in page
    assert page.count('class="job-row') == FEED_PAGE


def test_loader_fetches_the_rest_then_stops(conn, client):
    seed_jobs(conn, FEED_PAGE + 3)
    url = re.search(r'hx-get="(/feed/more[^"]+)"', client.get("/").text).group(1)
    more = client.get(url.replace("&amp;", "&")).text
    assert more.count('class="job-row') == 3
    assert "/feed/more" not in more


def test_loader_keeps_the_search(conn, client):
    seed_jobs(conn, FEED_PAGE * 2 + 4)
    page = client.get("/feed", params={"q": "python role"}).text
    assert "q=python%20role" in page


def test_short_feed_has_no_loader(conn, client):
    seed_jobs(conn, 3)
    assert "/feed/more" not in client.get("/").text


def test_first_page_uses_the_feed_index(conn):
    seed_jobs(conn, 3)
    plan = conn.execute(
        """EXPLAIN QUERY PLAN SELECT j.id FROM jobs j
           JOIN companies c ON c.id = j.company_id
           WHERE j.gate_failed = 0 AND j.dismissed = 0 AND j.score IS NOT NULL
             AND c.ats_type != 'manual'
           ORDER BY j.score DESC, j.id LIMIT 50"""
    ).fetchall()
    assert any("idx_jobs_feed" in row["detail"] for row in plan)


def test_refresh_updates_the_count_beside_the_heading(conn, client):
    seed_jobs(conn, 4)
    assert 'id="match-count" class="count-note" hx-swap-oob="true"' in client.get("/feed").text
    assert 'hx-swap-oob="true">4<' not in client.get("/").text


def test_breakdown_lists_matched_and_missing_keywords_as_chips(conn, client):
    ids = seed_jobs(conn, 1)
    breakdown = {
        "score": 12, "subtotal": 12, "seniority": "mid", "multipliers": [],
        "components": [{"category": "preferred", "points": 12, "reason": "preferred keyword 'rest'"}],
        "coverage": {"preferred": {"matched": ["rest"], "missing": ["graphql"]}},
    }
    conn.execute("UPDATE jobs SET score_breakdown = ? WHERE id = ?", (json.dumps(breakdown), ids[0]))
    conn.commit()
    page = client.get(f"/jobs/{ids[0]}/detail").text
    assert '<span class="chip on">rest</span>' in page
    assert ">graphql</span>" in page
    assert "+12" in page
