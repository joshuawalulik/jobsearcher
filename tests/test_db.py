import sqlite3

import pytest

import db


@pytest.fixture
def conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    yield conn
    conn.close()


def test_schema_creates_all_tables(conn):
    assert db.table_names(conn) == ["applications", "companies", "email_suggestions", "events", "jobs", "sweep_runs"]


def test_init_db_is_idempotent(conn):
    db.init_db(conn)
    assert db.table_names(conn) == ["applications", "companies", "email_suggestions", "events", "jobs", "sweep_runs"]


def test_job_dedupe_key(conn):
    conn.execute(
        "INSERT INTO companies (name, ats_type, slug) VALUES ('Example', 'greenhouse', 'example')"
    )
    conn.execute(
        "INSERT INTO jobs (company_id, external_id, title) VALUES (1, 'ext-1', 'Engineer')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (company_id, external_id, title) VALUES (1, 'ext-1', 'Engineer dup')"
        )


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (company_id, external_id, title) VALUES (999, 'ext-1', 'Orphan')"
        )


def test_application_lane_constraint(conn):
    conn.execute(
        "INSERT INTO companies (name, ats_type, slug) VALUES ('Example', 'greenhouse', 'example')"
    )
    conn.execute(
        "INSERT INTO jobs (company_id, external_id, title) VALUES (1, 'ext-1', 'Engineer')"
    )
    conn.execute("INSERT INTO applications (job_id) VALUES (1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO applications (job_id, lane) VALUES (1, 'daydreaming')")


def test_applications_check_migration_to_ready_lanes(tmp_path):
    """A DB created with the pre-'ready' CHECK gets rebuilt in place; rows
    in the dropped screening/ghosted lanes map to their nearest survivor."""
    path = tmp_path / "old.db"
    conn = db.get_connection(path)
    conn.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            ats_type TEXT NOT NULL, slug TEXT NOT NULL, active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
            lane TEXT NOT NULL DEFAULT 'considering' CHECK (lane IN
                ('considering', 'applied', 'screening', 'interviewing',
                 'offer', 'rejected', 'ghosted')),
            resume_version TEXT, notes TEXT, next_action TEXT,
            next_action_due TEXT, applied_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO applications (id, job_id, lane, notes) VALUES (7, 1, 'applied', 'n');
        INSERT INTO applications (id, job_id, lane) VALUES (8, 1, 'screening');
        INSERT INTO applications (id, job_id, lane) VALUES (9, 1, 'ghosted');
    """)
    conn.commit()
    db.init_db(conn)
    # Rows survived with their ids; dropped lanes mapped, new lanes legal.
    row = conn.execute("SELECT * FROM applications WHERE id = 7").fetchone()
    assert row["lane"] == "applied" and row["notes"] == "n"
    assert conn.execute(
        "SELECT lane FROM applications WHERE id = 8"
    ).fetchone()["lane"] == "interviewing"
    assert conn.execute(
        "SELECT lane FROM applications WHERE id = 9"
    ).fetchone()["lane"] == "rejected"
    conn.execute("UPDATE applications SET lane = 'ready' WHERE id = 7")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE applications SET lane = 'ghosted' WHERE id = 7")
    conn.commit()
    conn.close()
