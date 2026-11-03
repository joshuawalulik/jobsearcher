"""Tests for company sync, job upsert/dedupe/aging, and sweep orchestration."""

import json

import pytest

import autosweep
import db
from scrapers import ashby, common, greenhouse, lever, sweep


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


def job(**overrides) -> dict:
    base = {
        "external_id": "j-1",
        "title": "Software Engineer",
        "location": "Remote",
        "department": "Engineering",
        "url": "https://example.com/j-1",
        "description": "Build things in python.",
        "posted_at": "2026-07-01 12:00:00",
    }
    return {**base, **overrides}


# --- sync_companies ---------------------------------------------------------

def test_sync_inserts_new_company_active(conn):
    sweep.sync_companies(conn, [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}])
    row = conn.execute("SELECT * FROM companies").fetchone()
    assert (row["name"], row["ats_type"], row["slug"], row["active"]) == (
        "Acme", "greenhouse", "acme", 1,
    )


def test_sync_updates_name_and_reactivates(conn):
    sweep.sync_companies(conn, [{"name": "Acme", "ats": "lever", "slug": "acme"}])
    conn.execute("UPDATE companies SET active = 0")
    conn.commit()
    sweep.sync_companies(conn, [{"name": "Acme Corp", "ats": "lever", "slug": "acme"}])
    rows = conn.execute("SELECT * FROM companies").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Acme Corp"
    assert rows[0]["active"] == 1


def test_sync_deactivates_companies_removed_from_yaml(conn):
    sweep.sync_companies(conn, [
        {"name": "Keep", "ats": "ashby", "slug": "keep"},
        {"name": "Drop", "ats": "ashby", "slug": "drop"},
    ])
    sweep.sync_companies(conn, [{"name": "Keep", "ats": "ashby", "slug": "keep"}])
    active = {
        row["slug"]: row["active"]
        for row in conn.execute("SELECT slug, active FROM companies").fetchall()
    }
    assert active == {"keep": 1, "drop": 0}


def test_sync_rejects_unknown_ats_before_writing(conn):
    with pytest.raises(ValueError, match="unknown ats"):
        sweep.sync_companies(conn, [
            {"name": "Good", "ats": "greenhouse", "slug": "good"},
            {"name": "Bad", "ats": "workday", "slug": "bad"},
        ])
    assert conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"] == 0


# --- upsert_jobs ------------------------------------------------------------

def company_id(conn, slug="acme") -> int:
    sweep.sync_companies(conn, [{"name": slug, "ats": "greenhouse", "slug": slug}])
    return conn.execute("SELECT id FROM companies WHERE slug = ?", (slug,)).fetchone()["id"]


def test_upsert_new_job_counts_as_new(conn):
    cid = company_id(conn)
    assert sweep.upsert_jobs(conn, cid, [job()]) == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["title"] == "Software Engineer"
    assert row["first_seen"] == row["last_seen"]


def test_upsert_dedupes_and_bumps_last_seen(conn):
    cid = company_id(conn)
    sweep.upsert_jobs(conn, cid, [job()])
    # Backdate to simulate a previous sweep (datetime('now') is second-granular).
    conn.execute("UPDATE jobs SET first_seen = '2020-01-01 00:00:00', last_seen = '2020-01-01 00:00:00'")
    conn.commit()

    new = sweep.upsert_jobs(conn, cid, [job(title="Senior Software Engineer")])

    assert new == 0
    rows = conn.execute("SELECT * FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "Senior Software Engineer"
    assert rows[0]["first_seen"] == "2020-01-01 00:00:00"
    assert rows[0]["last_seen"] > rows[0]["first_seen"]


def test_upsert_preserves_score_and_dismissed_on_update(conn):
    cid = company_id(conn)
    sweep.upsert_jobs(conn, cid, [job()])
    conn.execute("UPDATE jobs SET score = 85.0, dismissed = 1")
    conn.commit()
    sweep.upsert_jobs(conn, cid, [job(description="updated")])
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["score"] == 85.0
    assert row["dismissed"] == 1
    assert row["description"] == "updated"


def test_upsert_dedupes_duplicate_external_ids_within_one_payload(conn):
    cid = company_id(conn)
    new = sweep.upsert_jobs(conn, cid, [job(), job(title="Duplicate")])
    assert new == 1
    rows = conn.execute("SELECT * FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "Software Engineer"  # first occurrence wins


def test_same_external_id_under_different_companies_is_two_rows(conn):
    cid_a = company_id(conn, "acme")
    cid_b = company_id(conn, "beta")
    # sync_companies() in company_id() deactivated acme; both ids remain valid.
    sweep.upsert_jobs(conn, cid_a, [job()])
    sweep.upsert_jobs(conn, cid_b, [job()])
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 2


# --- main() orchestration ---------------------------------------------------

def test_main_sweeps_skips_failures_and_sleeps_between(conn, tmp_path, monkeypatch, capsys):
    profile = {
        "companies": [
            {"name": "A Good", "ats": "greenhouse", "slug": "good"},
            {"name": "B Bad", "ats": "lever", "slug": "bad"},
            {"name": "C Boom", "ats": "ashby", "slug": "boom"},
        ],
        "scraper": {"delay_seconds": 3, "timeout_seconds": 7, "user_agent": "test-agent"},
    }
    monkeypatch.setattr(sweep, "load_profile", lambda: profile)
    monkeypatch.setattr(db, "get_connection", lambda *a, **kw: conn)
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")

    fetch_kwargs = []

    def fake_greenhouse_fetch(slug, *, user_agent, timeout):
        fetch_kwargs.append((user_agent, timeout))
        return {"jobs": ["raw"]}

    def fake_lever_fetch(slug, *, user_agent, timeout):
        raise common.BoardNotFound(f"404 for {slug}")

    def fake_ashby_fetch(slug, *, user_agent, timeout):
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(greenhouse, "fetch", fake_greenhouse_fetch)
    monkeypatch.setattr(greenhouse, "normalize", lambda raw, slug=None: [job()])
    monkeypatch.setattr(lever, "fetch", fake_lever_fetch)
    monkeypatch.setattr(ashby, "fetch", fake_ashby_fetch)

    sleeps = []
    monkeypatch.setattr(sweep.time, "sleep", sleeps.append)

    sweep.main()

    # Good company's job landed; neither failure (expected or not) was fatal.
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1
    # User-Agent and timeout came from the profile; raw response was cached.
    assert fetch_kwargs == [("test-agent", 7)]
    assert (tmp_path / "cache" / "greenhouse_good.json").exists()
    # Exactly one delay between each pair of companies, with the profile's value.
    assert sleeps == [3, 3]

    out = capsys.readouterr().out
    assert "board not found" in out
    assert "unexpected error: adapter bug" in out
    assert "Swept 3 companies: 1 jobs (1 new), 2 errors" in out


# --- sweep_runs recording ---------------------------------------------------

def test_record_and_read_last_sweep_run(conn):
    db.record_sweep_run(
        conn, started_at="2026-08-04 12:00:00", companies=3, jobs_seen=40,
        jobs_new=2, errors=["Acme: boom"], rescore_total=40, rescore_passed=10,
        rescore_rejected=30,
    )
    db.record_sweep_run(
        conn, started_at="2026-08-04 16:00:00", companies=3, jobs_seen=41,
        jobs_new=1, errors=[],
    )
    row = db.last_sweep_run(conn)
    assert (row["started_at"], row["jobs_seen"], row["jobs_new"]) == (
        "2026-08-04 16:00:00", 41, 1,
    )
    assert row["errors"] is None  # empty error list stored as NULL
    assert row["rescore_total"] is None
    assert row["finished_at"]


def test_sweep_and_rescore_records_run(conn, monkeypatch):
    profile = {
        "companies": [],
        "scraper": {"delay_seconds": 0, "timeout_seconds": 5, "user_agent": "t"},
        "gates": None,  # unused: no jobs reach the gate loop
        "scoring": None,
    }
    monkeypatch.setattr(
        sweep, "run",
        lambda c, p: {"companies": 2, "seen": 7, "new": 3, "errors": ["Acme: boom"]},
    )
    summary = autosweep.sweep_and_rescore(conn, profile)

    assert summary["sweep"]["new"] == 3
    assert summary["rescore"]["total"] == 0  # empty DB rescored for real
    row = db.last_sweep_run(conn)
    assert (row["companies"], row["jobs_seen"], row["jobs_new"]) == (2, 7, 3)
    assert json.loads(row["errors"]) == ["Acme: boom"]
    assert (row["rescore_total"], row["rescore_passed"], row["rescore_rejected"]) == (0, 0, 0)
