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
    assert db.table_names(conn) == [
        "applications", "companies", "contact_applications", "contacts",
        "email_suggestions", "events", "interactions", "jobs", "sweep_runs",
    ]


def test_init_db_is_idempotent(conn):
    db.init_db(conn)
    assert db.table_names(conn) == [
        "applications", "companies", "contact_applications", "contacts",
        "email_suggestions", "events", "interactions", "jobs", "sweep_runs",
    ]


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


def _contact(conn, **overrides):
    fields = {"name": "Pat Doe", "company": "Acme Corp", "track": "agency",
              "source": "research", **overrides}
    return db.insert_contact(conn, **fields)


def test_contact_round_trip_allows_firm_only_rows(conn):
    contact_id = _contact(conn, name=None, company="Globex")
    row = db.get_contact(conn, contact_id)
    assert row["name"] is None
    assert row["company"] == "Globex"
    assert row["status"] == "Not started"


def test_contact_email_is_unique_ignoring_case(conn):
    _contact(conn, email="Pat@Example.com")
    with pytest.raises(sqlite3.IntegrityError):
        _contact(conn, email="pat@example.com")


def test_contacts_without_email_do_not_collide(conn):
    _contact(conn, name="Pat Doe")
    _contact(conn, name="Sam Roe")
    assert len(db.fetch_contacts(conn)) == 2


def test_contact_enums_match_the_schema(conn):
    for status in db.CONTACT_STATUSES:
        _contact(conn, status=status)
    for track in db.CONTACT_TRACKS:
        _contact(conn, track=track)
    for priority in db.CONTACT_PRIORITIES:
        _contact(conn, priority=priority)
    for source in db.CONTACT_SOURCES:
        _contact(conn, source=source)
    with pytest.raises(sqlite3.IntegrityError):
        _contact(conn, status="Ghosted")


def test_contact_writes_reject_unknown_fields(conn):
    contact_id = _contact(conn)
    with pytest.raises(ValueError):
        db.update_contact(conn, contact_id, nonsense="x")
    with pytest.raises(ValueError):
        db.insert_contact(conn, track="agency", source="research", nonsense="x")


def test_update_contact_reports_missing_row(conn):
    assert db.update_contact(conn, 999, notes="x") is False
    contact_id = _contact(conn)
    assert db.update_contact(conn, contact_id, notes="x") is True
    assert db.get_contact(conn, contact_id)["notes"] == "x"


def test_interactions_list_in_date_order(conn):
    contact_id = _contact(conn)
    other = _contact(conn, name="Sam Roe")
    db.add_interaction(conn, contact_id, "2026-03-05", "email", "in", "Reply")
    db.add_interaction(conn, contact_id, "2026-03-02", "email", "out", "First")
    db.add_interaction(conn, other, "2026-03-03", "call", "out", "Call")
    assert [r["summary"] for r in db.fetch_interactions(conn, contact_id)] == [
        "First", "Reply",
    ]
    assert len(db.fetch_interactions(conn)) == 3


def test_link_contact_application_updates_note(conn):
    contact_id = _contact(conn)
    conn.execute(
        "INSERT INTO companies (name, ats_type, slug) VALUES ('Acme', 'greenhouse', 'acme')"
    )
    job_id = conn.execute(
        "INSERT INTO jobs (company_id, external_id, title) VALUES (1, 'x', 'Engineer')"
    ).lastrowid
    app_id = db.create_application(conn, job_id)
    db.link_contact_application(conn, contact_id, app_id, "first")
    db.link_contact_application(conn, contact_id, app_id, "second")
    rows = conn.execute("SELECT note FROM contact_applications").fetchall()
    assert [r["note"] for r in rows] == ["second"]


def test_removed_contact_leaves_every_read(conn):
    keep = _contact(conn, name="Sam Roe")
    gone = _contact(conn)
    assert db.remove_contact(conn, gone) is True
    assert [r["id"] for r in db.fetch_contacts(conn)] == [keep]
    assert db.get_contact(conn, gone) is None
    assert len(db.fetch_contacts(conn, include_removed=True)) == 2


def test_remove_contact_reports_missing_row(conn):
    assert db.remove_contact(conn, 999) is False
    contact_id = _contact(conn)
    db.remove_contact(conn, contact_id)
    assert db.remove_contact(conn, contact_id) is False


def test_removed_contacts_email_can_be_added_again(conn):
    db.remove_contact(conn, _contact(conn, email="pat@example.com"))
    _contact(conn, email="PAT@example.com")
    assert len(db.fetch_contacts(conn)) == 1


OLD_CONTACTS_TABLE = """
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY, name TEXT, company TEXT, email TEXT, linkedin_url TEXT,
    track TEXT NOT NULL CHECK (track IN
        ('agency', 'company_recruiter', 'referral', 'platform')),
    kind_detail TEXT, priority TEXT, nyc_relevance TEXT, ask TEXT,
    status TEXT NOT NULL DEFAULT 'Not started', date_contacted TEXT,
    last_reply TEXT, next_step TEXT, notes TEXT, source TEXT NOT NULL,
    peer_history TEXT, verify_flag TEXT, gmail_draft_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX contacts_email_unique ON contacts (lower(email))
    WHERE email IS NOT NULL;
"""


@pytest.fixture
def old_contacts_db(tmp_path):
    """A DB as it looked before the mentor track, with linked rows."""
    conn = db.get_connection(tmp_path / "old.db")
    db.init_db(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE contacts")
    conn.executescript(OLD_CONTACTS_TABLE)
    conn.execute(
        """
        INSERT INTO contacts (id, name, company, email, track, source, peer_history)
        VALUES (7, 'Pat Doe', 'Acme', 'pat@example.com', 'referral', 'peer_list', 'History')
        """
    )
    conn.execute(
        "INSERT INTO companies (name, ats_type, slug) VALUES ('Acme', 'greenhouse', 'acme')"
    )
    job_id = conn.execute(
        "INSERT INTO jobs (company_id, external_id, title) VALUES (1, 'x', 'Engineer')"
    ).lastrowid
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    app_id = db.create_application(conn, job_id)
    db.add_interaction(conn, 7, "2026-03-02", "email", "out", "Hello")
    db.link_contact_application(conn, 7, app_id, "Submitted")
    yield conn
    conn.close()


def test_contacts_migration_keeps_rows_and_their_links(old_contacts_db):
    conn = old_contacts_db
    db.init_db(conn)
    row = db.get_contact(conn, 7)
    assert row["name"] == "Pat Doe"
    assert row["peer_history"] == "History"
    assert row["removed_at"] is None
    assert [i["summary"] for i in db.fetch_interactions(conn, 7)] == ["Hello"]
    assert conn.execute("SELECT note FROM contact_applications").fetchone()["note"] == "Submitted"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_contacts_migration_allows_the_new_track_and_keeps_rules(old_contacts_db):
    conn = old_contacts_db
    db.init_db(conn)
    db.update_contact(conn, 7, track="mentor")
    assert db.get_contact(conn, 7)["track"] == "mentor"
    with pytest.raises(sqlite3.IntegrityError):
        db.update_contact(conn, 7, track="nonsense")
    with pytest.raises(sqlite3.IntegrityError):
        _contact(conn, email="PAT@example.com")


def test_contacts_migration_adds_the_standing_referral_flag(old_contacts_db):
    db.init_db(old_contacts_db)
    assert db.get_contact(old_contacts_db, 7)["standing_referral"] == 0


def test_flag_is_added_to_a_table_that_already_has_the_mentor_track(conn):
    conn.execute("ALTER TABLE contacts DROP COLUMN standing_referral")
    contact_id = _contact(conn)
    db.init_db(conn)
    assert db.get_contact(conn, contact_id)["standing_referral"] == 0
    db.update_contact(conn, contact_id, standing_referral=1)
    assert db.get_contact(conn, contact_id)["standing_referral"] == 1


def test_contacts_migration_runs_once(old_contacts_db):
    conn = old_contacts_db
    db.init_db(conn)
    db.remove_contact(conn, 7)
    db.init_db(conn)
    assert db.get_contact(conn, 7) is None
