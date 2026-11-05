"""Route tests for the Phase 4 application tracker.

Same pattern as test_app.py: TestClient with the get_db dependency
overridden to per-request connections against a seeded tmp database.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import db
import email_sync
from app import app, get_db


def seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    ids = {}
    for key, title in [("payments", "Software Engineer, Payments"), ("platform", "Platform Engineer")]:
        cur = conn.execute(
            """INSERT INTO jobs (company_id, external_id, title, location, url,
               score, score_breakdown, gate_failed, dismissed)
               VALUES (1, ?, ?, 'New York, NY', 'https://example.com/job', 42.5, NULL, 0, 0)""",
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


def track(client, key="payments"):
    """Track a seeded job, returning the new application id."""
    assert client.post(f"/jobs/{client.job_ids[key]}/track").status_code == 200
    row = client.db.execute(
        "SELECT id FROM applications WHERE job_id = ?", (client.job_ids[key],)
    ).fetchone()
    return row["id"]


def test_track_creates_application_in_considering_with_event(client):
    app_id = track(client)
    row = client.db.execute(
        "SELECT lane, applied_at FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["lane"] == "considering"
    assert row["applied_at"] is None
    events = client.db.execute(
        "SELECT type, detail, occurred_at FROM events WHERE application_id = ?", (app_id,)
    ).fetchall()
    assert [(e["type"], e["detail"]) for e in events] == [("tracked", "considering")]
    assert events[0]["occurred_at"]  # occurred_at populated for Phase 5


def test_track_is_idempotent(client):
    track(client)
    assert client.post(f"/jobs/{client.job_ids['payments']}/track").status_code == 200
    count = client.db.execute("SELECT count(*) AS n FROM applications").fetchone()["n"]
    assert count == 1


def test_track_unknown_job_404s(client):
    assert client.post("/jobs/999999/track").status_code == 404


def test_feed_shows_track_button_then_lane_pill(client):
    assert "Track</button>" in client.get("/").text
    app_id = track(client)
    page = client.get("/").text
    # The pill mirrors the tracker lane, starting at Considering.
    assert ">Considering</a>" in page
    # The tracked card no longer offers a Track button; the untracked one does.
    assert page.count("Track</button>") == 1
    # Moving lanes on the tracker updates the feed pill too.
    client.post(f"/applications/{app_id}/lane", data={"lane": "applied"})
    assert ">Applied</a>" in client.get("/").text


def test_application_modal_shows_jd_breakdown_details_and_status(client):
    app_id = track(client)
    client.db.execute(
        "UPDATE jobs SET description = 'Full JD text MODAL-JD', score_breakdown = ? WHERE id = ?",
        (
            '{"score": 42.5, "subtotal": 50, "seniority": "mid",'
            ' "components": [{"reason": "title matches", "points": 30}], "multipliers": []}',
            client.job_ids["payments"],
        ),
    )
    client.db.execute(
        "UPDATE applications SET notes = 'Ping recruiter MODAL-NOTE' WHERE id = ?",
        (app_id,),
    )
    client.db.commit()

    page = client.get(f"/applications/{app_id}/modal").text
    assert "Software Engineer, Payments" in page
    assert "Full JD text MODAL-JD" in page          # saved job description
    assert "title matches" in page                  # score breakdown
    assert "Ping recruiter MODAL-NOTE" in page      # details
    assert ">Considering</span>" in page            # status (lane)
    assert 'href="https://example.com/job"' in page  # link to the posting


def test_application_modal_unknown_id_404s(client):
    assert client.get("/applications/999999/modal").status_code == 404


def test_tracker_card_opens_modal_on_click(client):
    app_id = track(client)
    page = client.get("/tracker").text
    assert f'hx-get="/applications/{app_id}/modal"' in page
    assert 'id="modal"' in page
    assert "opensAppModal" in page  # click filter skips the card's controls


def test_tracker_page_renders_lanes_and_card(client):
    track(client)
    page = client.get("/tracker").text
    for lane in db.LANES:
        assert lane in page
    assert "Software Engineer, Payments" in page
    assert "Platform Engineer" not in page  # untracked job stays off the board


def test_lane_change_logs_event(client):
    app_id = track(client)
    page = client.post(f"/applications/{app_id}/lane", data={"lane": "ready"}).text
    assert 'id="board"' in page
    row = client.db.execute(
        "SELECT lane, applied_at FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["lane"] == "ready"
    assert row["applied_at"] is None  # only the applied lane stamps it
    detail = client.db.execute(
        "SELECT detail FROM events WHERE application_id = ? AND type = 'lane_change'",
        (app_id,),
    ).fetchone()["detail"]
    assert detail == "considering -> ready"


def test_move_to_applied_stamps_applied_at_once(client):
    app_id = track(client)
    client.post(f"/applications/{app_id}/lane", data={"lane": "applied"})
    stamped = client.db.execute(
        "SELECT applied_at FROM applications WHERE id = ?", (app_id,)
    ).fetchone()["applied_at"]
    assert stamped is not None
    # Bounce out and back: the original stamp survives.
    client.post(f"/applications/{app_id}/lane", data={"lane": "interviewing"})
    client.post(f"/applications/{app_id}/lane", data={"lane": "applied"})
    row = client.db.execute(
        "SELECT applied_at FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["applied_at"] == stamped
    changes = client.db.execute(
        "SELECT detail FROM events WHERE application_id = ? AND type = 'lane_change' ORDER BY id",
        (app_id,),
    ).fetchall()
    assert [c["detail"] for c in changes] == [
        "considering -> applied",
        "applied -> interviewing",
        "interviewing -> applied",
    ]


def test_same_lane_move_logs_no_event(client):
    app_id = track(client)
    assert client.post(f"/applications/{app_id}/lane", data={"lane": "considering"}).status_code == 200
    n = client.db.execute(
        "SELECT count(*) AS n FROM events WHERE type = 'lane_change'"
    ).fetchone()["n"]
    assert n == 0


def test_lane_change_rejects_bad_input(client):
    app_id = track(client)
    assert client.post(f"/applications/{app_id}/lane", data={"lane": "on-hold"}).status_code == 422
    assert client.post("/applications/999999/lane", data={"lane": "applied"}).status_code == 404


def test_untrack_deletes_application_and_events(client):
    app_id = track(client)
    page = client.post(f"/applications/{app_id}/untrack")
    assert page.status_code == 200
    assert 'id="board"' in page.text
    assert client.db.execute("SELECT count(*) AS n FROM applications").fetchone()["n"] == 0
    assert client.db.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0
    # The job is a plain match again: Track button back for both seeded jobs.
    assert client.get("/").text.count("Track</button>") == 2


def test_untrack_refused_once_past_considering(client):
    app_id = track(client)
    client.post(f"/applications/{app_id}/lane", data={"lane": "applied"})
    assert client.post(f"/applications/{app_id}/untrack").status_code == 409
    # Moved back to considering, untrack works (and discards the history).
    client.post(f"/applications/{app_id}/lane", data={"lane": "considering"})
    assert client.post(f"/applications/{app_id}/untrack").status_code == 200
    assert client.db.execute("SELECT count(*) AS n FROM applications").fetchone()["n"] == 0


def test_untrack_unknown_application_404s(client):
    assert client.post("/applications/999999/untrack").status_code == 404


def test_considering_card_shows_untrack_button(client):
    app_id = track(client)
    assert "Untrack" in client.get("/tracker").text
    client.post(f"/applications/{app_id}/lane", data={"lane": "applied"})
    assert "Untrack" not in client.get("/tracker").text


def test_details_save_persists_fields(client):
    app_id = track(client)
    page = client.post(
        f"/applications/{app_id}",
        data={
            "notes": "Referred by Sam",
            "resume_version": "fullstack-v3",
            "next_action": "Follow up with recruiter",
            "next_action_due": "2026-07-25",
            "applied_at": "2026-07-18",
        },
    ).text
    assert "Referred by Sam" in page
    assert "saved" in page
    row = client.db.execute(
        "SELECT * FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["resume_version"] == "fullstack-v3"
    assert row["next_action"] == "Follow up with recruiter"
    assert row["next_action_due"] == "2026-07-25"
    assert row["applied_at"] == "2026-07-18"


def test_details_save_blank_fields_store_null(client):
    app_id = track(client)
    client.post(
        f"/applications/{app_id}",
        data={"notes": "  ", "resume_version": "", "next_action": "",
              "next_action_due": "", "applied_at": ""},
    )
    row = client.db.execute(
        "SELECT notes, resume_version, next_action FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["notes"] is None
    assert row["resume_version"] is None
    assert row["next_action"] is None


def test_details_save_unknown_application_404s(client):
    assert client.post("/applications/999999", data={"notes": "x"}).status_code == 404


def test_tracking_an_unscored_job_scores_it(client):
    # Gate-rejected jobs carry no score; tracking one (via the URL-lookup
    # flow) must score it so the tracker card shows a fit number.
    cur = client.db.execute(
        """INSERT INTO jobs (company_id, external_id, title, description, score,
           gate_failed, gate_reason, dismissed)
           VALUES (1, 'old', 'Software Engineer', 'react and typescript daily. ' || printf('%.*c', 400, 'x'),
                   NULL, 1, 'age: posted 260 days ago (max 30)', 0)"""
    )
    client.db.commit()
    job_id = cur.lastrowid
    assert client.post(f"/jobs/{job_id}/track").status_code == 200
    row = client.db.execute(
        "SELECT score, score_breakdown, gate_failed FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["score"] is not None
    assert row["score_breakdown"] is not None
    assert row["gate_failed"] == 1  # gate verdict is history, not erased


def test_tracker_card_shows_score_breakdown(client):
    import json
    breakdown = {
        "score": 42.5, "subtotal": 50.0, "seniority": "mid",
        "components": [{"reason": "title matches target 'Software Engineer'", "points": 30}],
        "multipliers": [{"reason": "sparse description x0.85", "factor": 0.85}],
    }
    client.db.execute(
        "UPDATE jobs SET score_breakdown = ? WHERE id = ?",
        (json.dumps(breakdown), client.job_ids["payments"]),
    )
    client.db.commit()
    app_id = track(client)
    page = client.get("/tracker").text
    assert "Score breakdown" in page
    assert "title matches target" in page
    # The details-save response re-renders the card with the breakdown too.
    saved = client.post(
        f"/applications/{app_id}",
        data={"notes": "n", "resume_version": "", "next_action": "",
              "next_action_due": "", "applied_at": ""},
    ).text
    assert "Score breakdown" in saved


# --- email sync UI ----------------------------------------------------------

def suggest(client, app_id, lane="rejected", reason='matched "not moving forward"'):
    db.add_email_suggestion(
        client.db, application_id=app_id, msg_uid="42",
        from_addr="r@acme.com", subject="Update on your application",
        received_at="2026-09-10 16:30:00", suggested_lane=lane, reason=reason,
        body="FULL-EMAIL-BODY here.", message_id="<mid42@acme.com>",
    )
    return client.db.execute("SELECT id FROM email_suggestions").fetchone()["id"]


def test_tracker_shows_pending_suggestion_with_actions(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    sid = suggest(client, app_id)
    page = client.get("/tracker").text
    assert "suggests <strong>Rejected</strong>" in page
    # The stored mail is inspectable: full body + a Gmail deep link.
    assert "FULL-EMAIL-BODY here." in page
    assert "rfc822msgid%3Amid42%40acme.com" in page
    assert "Open in Gmail" in page
    assert "not moving forward" in page
    assert "Update on your application" in page
    assert f"/email/suggestions/{sid}/accept" in page
    assert f"/email/suggestions/{sid}/dismiss" in page
    # The tracker header offers the sync button.
    assert 'hx-post="/email/sync"' in page


def test_card_shows_label_sender_and_deciding_sentence(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    db.add_email_suggestion(
        client.db, application_id=app_id, msg_uid="7",
        from_addr="jane.doe@acme.com", from_name="Jane Doe", subject="Quick question",
        received_at="2026-09-10 16:30:00", suggested_lane=None,
        reason="from a person, not an automated sender", kind="person",
        evidence="Do you have time to talk this week?",
    )
    page = client.get("/tracker").text
    assert '<span class="kind-pill">Person</span>' in page
    assert "Jane Doe &lt;jane.doe@acme.com&gt;" in page
    assert "Do you have time to talk this week?" in page
    assert "Save reply" in page
    modal = client.get(f"/applications/{app_id}/modal").text
    assert "Jane Doe &lt;jane.doe@acme.com&gt;" in modal
    assert "Do you have time to talk this week?" in modal


def test_old_rows_without_a_kind_still_render(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    suggest(client, app_id)  # no kind, no evidence, no sender name
    page = client.get("/tracker").text
    assert '<span class="kind-pill">Rejection</span>' in page
    assert "r@acme.com" in page
    assert "email-evidence" not in page


def test_filed_receipt_is_reply_history_not_a_card(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    db.add_email_suggestion(
        client.db, application_id=app_id, msg_uid="8", from_addr="no-reply@acme.com",
        subject="Thanks for applying", received_at="2026-09-10 16:30:00",
        suggested_lane=None, reason='automatic receipt: "received your application"',
        kind="receipt", status="accepted",
    )
    page = client.get("/tracker").text
    assert '<span class="kind-pill">Receipt</span>' in page
    assert "Save reply" not in page
    assert "/email/suggestions/" not in page


def test_ignored_mail_is_listed_with_its_reason(client):
    assert "Ignored mail" not in client.get("/tracker").text
    db.add_ignored_email(
        client.db, msg_uid="9", company="Acme", from_name=None,
        from_addr="no-reply@acme.com", subject="Your verification code",
        received_at="2026-09-10 16:30:00", message_id="<mid9@acme.com>",
        reason='ignored: mentions "verification code"',
    )
    page = client.get("/tracker").text
    assert "Ignored mail (1)" in page
    assert "Your verification code" in page
    assert "mentions &#34;verification code&#34;" in page
    assert "rfc822msgid%3Amid9%40acme.com" in page


def test_accept_suggestion_moves_lane_and_resolves(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    sid = suggest(client, app_id)
    page = client.post(f"/email/suggestions/{sid}/accept").text
    row = client.db.execute(
        "SELECT lane FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["lane"] == "rejected"
    status = client.db.execute(
        "SELECT status FROM email_suggestions WHERE id = ?", (sid,)
    ).fetchone()["status"]
    assert status == "accepted"
    assert "not moving forward" not in page  # resolved: no longer rendered
    # The lane move went through the normal event log.
    detail = client.db.execute(
        "SELECT detail FROM events WHERE type = 'lane_change' ORDER BY id DESC"
    ).fetchone()["detail"]
    assert detail == "applied -> rejected"
    # Accepting twice 404s (no longer pending).
    assert client.post(f"/email/suggestions/{sid}/accept").status_code == 404


def test_dismiss_suggestion_keeps_lane(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    sid = suggest(client, app_id)
    page = client.post(f"/email/suggestions/{sid}/dismiss").text
    assert "not moving forward" not in page
    row = client.db.execute(
        "SELECT lane FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["lane"] == "applied"  # unchanged


def test_save_reply_only_suggestion_keeps_history_without_lane_move(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    sid = suggest(client, app_id, lane=None, reason='reply mentions "your application"')
    page = client.get("/tracker").text
    assert ">Save reply" in page  # lane-less: keep, don't move
    board = client.post(f"/email/suggestions/{sid}/accept").text
    row = client.db.execute(
        "SELECT lane FROM applications WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["lane"] == "applied"  # unchanged — nothing to move to
    # The kept reply now renders as history in the card's Details section…
    assert "Email replies" in board
    assert "Update on your application" in board
    # …and in the modal, with sender and reason.
    modal = client.get(f"/applications/{app_id}/modal").text
    assert "Email replies" in modal
    assert "r@acme.com" in modal
    assert "reply mentions" in modal


def test_accepted_lane_move_shows_in_history_dismissed_hidden(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    sid = suggest(client, app_id)  # suggests rejected
    client.post(f"/email/suggestions/{sid}/accept")
    page = client.get("/tracker").text
    assert "Email replies" in page
    assert "moved to rejected" not in page  # card history is compact
    modal = client.get(f"/applications/{app_id}/modal").text
    assert "moved to rejected" in modal

    # A dismissed suggestion never shows up as history.
    db.add_email_suggestion(
        client.db, application_id=app_id, msg_uid="99", from_addr="spam@x.com",
        subject="NEWSLETTER-ROW", received_at="2026-09-12 08:00:00",
        suggested_lane=None, reason='reply mentions "your application"',
    )
    sid2 = client.db.execute(
        "SELECT id FROM email_suggestions WHERE msg_uid = '99'"
    ).fetchone()["id"]
    client.post(f"/email/suggestions/{sid2}/dismiss")
    assert "NEWSLETTER-ROW" not in client.get("/tracker").text
    assert "NEWSLETTER-ROW" not in client.get(f"/applications/{app_id}/modal").text


def test_modal_labels_pending_suggestions(client):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    suggest(client, app_id)
    modal = client.get(f"/applications/{app_id}/modal").text
    assert "pending" in modal
    assert "suggests rejected" in modal


def test_email_sync_unconfigured_shows_setup_hint(client, monkeypatch):
    monkeypatch.setattr(email_sync, "load_secrets", lambda *a, **kw: None)
    page = client.post("/email/sync").text
    assert "secrets.yaml" in page
    assert "error" in page


def test_email_sync_endpoint_runs_sync_and_refreshes_board(client, monkeypatch):
    app_id = track(client)
    db.set_application_lane(client.db, app_id, "applied")
    monkeypatch.setattr(
        email_sync, "load_secrets", lambda *a, **kw: {"username": "u", "app_password": "p"}
    )

    def fake_sync(conn, profile, *, secrets=None, imap=None, now=None):
        db.add_email_suggestion(
            conn, application_id=app_id, msg_uid="9", from_addr="r@x.com",
            subject="Phone screen?", received_at="2026-09-11 10:00:00",
            suggested_lane="interviewing", reason='matched "phone screen"',
        )
        return {"companies": 1, "emails_scanned": 3, "new": 1, "receipts": 0, "ignored": 0}

    monkeypatch.setattr(email_sync, "sync", fake_sync)
    page = client.post("/email/sync").text
    assert "1 company: 3 emails scanned, 1 new suggestion." in page
    # Out-of-band board refresh carries the new suggestion.
    assert 'hx-swap-oob="true"' in page
    assert "phone screen" in page
