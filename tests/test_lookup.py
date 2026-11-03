"""URL lookup for manual job entry: parsing, fetching, and the /jobs/lookup route."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import db
from app import app, get_db
from scrapers import common, lookup

LEVER_ID = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://job-boards.greenhouse.io/mongodb/jobs/8054991",
         ("greenhouse", "mongodb", "8054991")),
        ("https://boards.greenhouse.io/stripe/jobs/123?gh_src=abc",
         ("greenhouse", "stripe", "123")),
        ("https://job-boards.eu.greenhouse.io/adyen/jobs/99",
         ("greenhouse", "adyen", "99")),
        ("https://boards.greenhouse.io/embed/job_app?for=mongodb&token=8054991",
         ("greenhouse", "mongodb", "8054991")),
        ("https://boards.greenhouse.io/embed/job_app?token=8054991&for=mongodb",
         ("greenhouse", "mongodb", "8054991")),
        (f"https://jobs.lever.co/spotify/{LEVER_ID}", ("lever", "spotify", LEVER_ID)),
        (f"https://jobs.eu.lever.co/spotify/{LEVER_ID}/apply",
         ("lever", "spotify", LEVER_ID)),
        (f"https://jobs.ashbyhq.com/openai/{LEVER_ID}", ("ashby", "openai", LEVER_ID)),
        # Unrecognized: company sites, LinkedIn, bare boards
        ("https://www.mongodb.com/careers/job/?gh_jid=8054991", None),
        ("https://www.linkedin.com/jobs/view/3941780031", None),
        ("https://job-boards.greenhouse.io/mongodb", None),
        ("", None),
    ],
)
def test_parse_job_url(url, expected):
    assert lookup.parse_job_url(url) == expected


def test_fetch_posting_greenhouse_single(monkeypatch):
    raw = {
        "id": 8054991,
        "title": "Software Engineer 3",
        "location": {"name": "New York City"},
        "absolute_url": "https://www.mongodb.com/careers/job/?gh_jid=8054991",
        "content": "Build &lt;b&gt;things&lt;/b&gt; with react.",
    }
    monkeypatch.setattr(common, "fetch_json", lambda url, **kw: raw)
    posting = lookup.fetch_posting("greenhouse", "mongodb", "8054991",
                                   user_agent="t", timeout=5)
    assert posting["title"] == "Software Engineer 3"
    assert posting["url"] == "https://www.mongodb.com/careers/job/?gh_jid=8054991"
    assert "react" in posting["description"]


def test_fetch_posting_lever_single(monkeypatch):
    raw = {"id": LEVER_ID, "text": "Backend Engineer",
           "categories": {"location": "NYC"}, "descriptionPlain": "Go and Postgres.",
           "hostedUrl": f"https://jobs.lever.co/spotify/{LEVER_ID}"}
    monkeypatch.setattr(common, "fetch_json", lambda url, **kw: raw)
    posting = lookup.fetch_posting("lever", "spotify", LEVER_ID, user_agent="t", timeout=5)
    assert posting["title"] == "Backend Engineer"


def test_fetch_posting_ashby_picks_from_board(monkeypatch):
    board = {"jobs": [
        {"id": "other", "title": "Nope", "descriptionPlain": "x"},
        {"id": LEVER_ID, "title": "Platform Engineer", "location": "Remote",
         "descriptionPlain": "Kubernetes.", "jobUrl": "https://jobs.ashbyhq.com/x/y"},
    ]}
    monkeypatch.setattr(common, "fetch_json", lambda url, **kw: board)
    posting = lookup.fetch_posting("ashby", "openai", LEVER_ID, user_agent="t", timeout=5)
    assert posting["title"] == "Platform Engineer"
    assert lookup.fetch_posting("ashby", "openai", "missing", user_agent="t", timeout=5) is None


# --- route -------------------------------------------------------------------

def seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, score, gate_failed, dismissed)
           VALUES (1, '123456', 'Software Engineer, Payments', 42.5, 0, 0)"""
    )
    conn.commit()


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test.db"
    conn = db.get_connection(db_path)
    db.init_db(conn)
    seed(conn)
    client = TestClient(app)
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


def test_lookup_unrecognized_url_shows_error(client):
    page = client.get("/jobs/lookup", params={"url": "https://linkedin.com/jobs/view/1"}).text
    assert "Couldn&#39;t recognize that URL" in page or "Couldn't recognize that URL" in page
    assert 'name="company"' in page  # manual form still offered


def test_lookup_existing_job_offers_track(client):
    page = client.get(
        "/jobs/lookup",
        params={"url": "https://job-boards.greenhouse.io/acme/jobs/123456"},
    ).text
    assert "Already in the tool" in page
    assert "/jobs/1/track" in page.replace(
        f"/jobs/{client.db.execute('SELECT id FROM jobs').fetchone()['id']}/track",
        "/jobs/1/track",
    )


def test_lookup_existing_tracked_job_says_so(client):
    job_id = client.db.execute("SELECT id FROM jobs").fetchone()["id"]
    client.post(f"/jobs/{job_id}/track")
    page = client.get(
        "/jobs/lookup",
        params={"url": "https://job-boards.greenhouse.io/acme/jobs/123456"},
    ).text
    assert "already" in page.lower() and "tracker" in page.lower()
    assert "Track it" not in page


def test_lookup_success_prefills_form(client, monkeypatch):
    posting = {"external_id": "777", "title": "Staff Nothing", "location": "NYC",
               "url": "https://job-boards.greenhouse.io/newco/jobs/777",
               "description": "React and node.", "department": None, "posted_at": None}
    monkeypatch.setattr(lookup, "fetch_posting", lambda *a, **kw: posting)
    page = client.get(
        "/jobs/lookup",
        params={"url": "https://job-boards.greenhouse.io/newco/jobs/777"},
    ).text
    assert 'value="Newco"' in page  # slug prettified (company not in watchlist)
    assert 'value="Staff Nothing"' in page
    assert "React and node." in page
    assert "review, then add" in page


def test_lookup_fetch_error_is_shown(client, monkeypatch):
    def boom(*a, **kw):
        raise common.BoardNotFound("404 from upstream")
    monkeypatch.setattr(lookup, "fetch_posting", boom)
    page = client.get(
        "/jobs/lookup",
        params={"url": "https://job-boards.greenhouse.io/newco/jobs/777"},
    ).text
    assert "Fetch failed" in page and "404 from upstream" in page


def test_lookup_existing_gate_failed_job_warns(client):
    client.db.execute(
        """INSERT INTO jobs (company_id, external_id, title, score, gate_failed,
           gate_reason, dismissed)
           VALUES (1, '654321', 'Old Software Engineer', NULL, 1,
                   'age: posted 260 days ago (max 30)', 0)"""
    )
    client.db.commit()
    page = client.get(
        "/jobs/lookup",
        params={"url": "https://job-boards.greenhouse.io/acme/jobs/654321"},
    ).text
    assert "Already in the tool" in page
    assert "gate-rejected" in page and "260 days" in page
    assert "Track it" in page
