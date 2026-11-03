import json

import pytest

import db
import mcp_server


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.get_connection(path)
    db.init_db(conn)
    yield conn
    conn.close()


def _make_tracked_job(conn, title="Engineer", *, notes=None, lane=None):
    conn.execute(
        "INSERT OR IGNORE INTO companies (name, ats_type, slug) VALUES ('Example', 'greenhouse', 'example')"
    )
    job_id = conn.execute(
        """
        INSERT INTO jobs (company_id, external_id, title, location, url,
                          description, score, score_breakdown)
        VALUES (1, ?, ?, 'NYC', 'https://example.com/j', 'Build things.', 72.5, ?)
        """,
        (f"ext-{title}", title, json.dumps({"required": {"matched": ["python"]}})),
    ).lastrowid
    app_id = db.create_application(conn, job_id)
    if notes is not None:
        db.update_application_details(
            conn, app_id, notes=notes, resume_version=None,
            next_action=None, next_action_due=None, applied_at=None,
        )
    if lane is not None:
        db.set_application_lane(conn, app_id, lane)
    return app_id


def test_append_note_to_empty(test_db):
    app_id = _make_tracked_job(test_db)
    assert db.append_application_note(test_db, app_id, "Draft cover letter")
    row = db.get_application(test_db, app_id)
    assert row["notes"] == "Draft cover letter"


def test_append_note_preserves_existing(test_db):
    app_id = _make_tracked_job(test_db, notes="hand-written\n")
    assert db.append_application_note(test_db, app_id, "workflow output")
    row = db.get_application(test_db, app_id)
    assert row["notes"] == "hand-written\n\nworkflow output"


def test_append_note_unknown_app(test_db):
    assert not db.append_application_note(test_db, 999, "text")


def test_list_considering_jobs(test_db):
    a1 = _make_tracked_job(test_db, "Backend Engineer")
    _make_tracked_job(test_db, "Applied Role", lane="applied")
    a3 = _make_tracked_job(test_db, "Full Stack Engineer", notes="done")

    jobs = mcp_server.list_considering_jobs()
    assert {j["app_id"] for j in jobs} == {a1, a3}
    by_id = {j["app_id"]: j for j in jobs}
    assert by_id[a1]["has_notes"] is False
    assert by_id[a3]["has_notes"] is True
    assert by_id[a1]["company"] == "Example"
    assert by_id[a1]["score"] == 72.5


def test_get_job(test_db):
    app_id = _make_tracked_job(test_db, notes="my notes")
    job = mcp_server.get_job(app_id)
    assert job["description"] == "Build things."
    assert job["score_breakdown"] == {"required": {"matched": ["python"]}}
    assert job["notes"] == "my notes"
    assert job["lane"] == "considering"


def test_get_job_unknown(test_db):
    with pytest.raises(ValueError):
        mcp_server.get_job(999)


def test_save_application_note(test_db):
    app_id = _make_tracked_job(test_db)
    mcp_server.save_application_note(app_id, "  tailored bullets  ")
    row = db.get_application(test_db, app_id)
    assert row["notes"] == "tailored bullets"


def test_save_application_note_rejects_empty(test_db):
    app_id = _make_tracked_job(test_db)
    with pytest.raises(ValueError):
        mcp_server.save_application_note(app_id, "   ")
