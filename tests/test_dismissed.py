"""Recently-dismissed page: dismiss stamps a timestamp, restore undoes it.

Same pattern as test_tracker.py: TestClient with the get_db dependency
overridden against a seeded tmp database.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import db
from app import app, get_db


def seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    ids = {}
    for key, title in [("payments", "Software Engineer, Payments"), ("platform", "Platform Engineer")]:
        cur = conn.execute(
            """INSERT INTO jobs (company_id, external_id, title, location, url,
               score, gate_failed, dismissed)
               VALUES (1, ?, ?, 'New York, NY', 'https://example.com/job', 42.5, 0, 0)""",
            (key, title),
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


def test_dismiss_stamps_timestamp(client):
    job_id = client.job_ids["payments"]
    assert client.post(f"/jobs/{job_id}/dismiss").status_code == 200
    row = client.db.execute(
        "SELECT dismissed, dismissed_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["dismissed"] == 1
    assert row["dismissed_at"] is not None


def test_dismissed_page_lists_dismissed_jobs_only(client):
    client.post(f"/jobs/{client.job_ids['payments']}/dismiss")
    page = client.get("/dismissed").text
    assert "Software Engineer, Payments" in page
    assert "Platform Engineer" not in page
    assert "Restore" in page


def test_empty_state(client):
    assert "Nothing dismissed." in client.get("/dismissed").text


def test_restore_returns_job_to_feed(client):
    job_id = client.job_ids["payments"]
    client.post(f"/jobs/{job_id}/dismiss")
    assert "Software Engineer, Payments" not in client.get("/").text
    assert client.post(f"/jobs/{job_id}/restore").status_code == 200
    row = client.db.execute(
        "SELECT dismissed, dismissed_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["dismissed"] == 0
    assert row["dismissed_at"] is None
    assert "Software Engineer, Payments" in client.get("/").text
    assert "Nothing dismissed." in client.get("/dismissed").text


def test_restore_unknown_job_404s(client):
    assert client.post("/jobs/999999/restore").status_code == 404


def test_init_db_migrates_pre_dismissed_at_schema(tmp_path):
    # A DB created before dismissed_at existed: CREATE TABLE IF NOT EXISTS
    # skips the existing table, so init_db must ALTER it in.
    conn = db.get_connection(tmp_path / "old.db")
    conn.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, dismissed INTEGER NOT NULL DEFAULT 0);"
    )
    db.init_db(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "dismissed_at" in columns
    db.init_db(conn)  # second run stays idempotent
    conn.close()
