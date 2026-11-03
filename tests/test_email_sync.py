"""Tests for inbox → tracker suggestions: classification, message
parsing, application matching, and the sync loop over a fake IMAP."""

from email.message import EmailMessage

import pytest

import db
import email_sync

CLASSIFY = {
    "rejected": ["not moving forward", "unfortunately"],
    "offer": ["offer letter"],
    "interviewing": ["technical interview", "onsite", "phone screen", "schedule a call"],
}
MARKERS = ["your application", "thank you for applying"]

EMAIL_CFG = {
    "enabled": True,
    "imap_host": "imap.example.com",
    "folder": "INBOX",
    "lookback_days": 14,
    "sync_lanes": ["applied", "interviewing", "offer"],
    "classify": CLASSIFY,
    "reply_markers": MARKERS,
}


# --- classify / reply_marker ------------------------------------------------

def test_classify_ties_keep_bucket_order():
    # Rejection and interviewing score 1 each — rejected is listed first.
    lane, reason = email_sync.classify(
        "Update", "After your technical interview we are not moving forward.",
        CLASSIFY,
    )
    assert lane == "rejected"
    assert reason == 'matched "not moving forward" — over interviewing'


def test_classify_best_scoring_bucket_beats_a_stray_phrase():
    # A scheduling email with one incidental "unfortunately" is not a rejection.
    lane, reason = email_sync.classify(
        "Next steps",
        "Unfortunately Thursday is out for the hiring manager — "
        "please pick a slot for your technical interview and the onsite.",
        CLASSIFY,
    )
    assert lane == "interviewing"
    assert reason == 'matched "technical interview", "onsite" — over rejected'


def test_classify_subject_hits_weigh_extra():
    # 1 body hit for rejected vs a subject hit (x2) for interviewing.
    lane, reason = email_sync.classify(
        "Phone screen with Acme",
        "Unfortunately the recruiter you emailed has left; I'll take over.",
        CLASSIFY,
    )
    assert lane == "interviewing"
    assert reason == 'matched "phone screen" (subject) — over rejected'


def test_classify_ignores_quoted_thread_history():
    # The quoted interview invite below the reply must not vote.
    lane, reason = email_sync.classify(
        "Re: Interview",
        "We are not moving forward.\n\n"
        "On Tue, Sep 22, 2026 at 9:00 AM Recruiter <r@acme.com> wrote:\n"
        "> Let's schedule your technical interview and onsite\n"
        "> with the hiring manager for a coding challenge.",
        CLASSIFY,
    )
    assert lane == "rejected"
    assert reason == 'matched "not moving forward"'


def test_classify_is_case_insensitive_and_none_when_no_phrase():
    assert email_sync.classify("PHONE SCREEN next week", "", CLASSIFY)[0] == "interviewing"
    assert email_sync.classify("Quarterly product newsletter", "Big news!", CLASSIFY) == (None, None)


def test_strip_quoted_cuts_forward_markers_and_quote_lines():
    body = "New part\n> quoted line\n---------- Forwarded message ----------\nold part"
    assert email_sync.strip_quoted(body) == "New part"


def test_reply_marker_gates_unclassified_mail():
    assert email_sync.reply_marker("Update on your application", MARKERS) == "your application"
    assert email_sync.reply_marker("50% off this weekend only", MARKERS) is None


# --- parse_message ----------------------------------------------------------

def raw_email(subject="Hello", body="Plain body", html=None, sender="Recruiter <r@acme.com>"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Thu, 10 Sep 2026 12:30:00 -0400"
    msg["Message-ID"] = "<abc123@mail.acme.com>"
    if html is not None:
        msg.set_content(body) if body else None
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(body)
    return bytes(msg)


def test_parse_message_plain():
    parsed = email_sync.parse_message("7", raw_email())
    assert parsed["uid"] == "7"
    assert parsed["from_addr"] == "r@acme.com"
    assert parsed["subject"] == "Hello"
    assert parsed["received_at"] == "2026-09-10 16:30:00"  # UTC
    assert parsed["body"].strip() == "Plain body"
    assert parsed["message_id"] == "<abc123@mail.acme.com>"


def test_parse_message_html_only_and_encoded_subject():
    msg = EmailMessage()
    msg["From"] = "r@acme.com"
    msg["Subject"] = "=?utf-8?q?Interview_=E2=80=94_next_steps?="
    msg.add_alternative("<p>Schedule a <b>call</b></p>", subtype="html")
    parsed = email_sync.parse_message("1", bytes(msg))
    assert parsed["subject"] == "Interview — next steps"
    assert "Schedule a call" in parsed["body"]
    assert parsed["received_at"] is None  # no Date header
    assert parsed["message_id"] is None  # no Message-ID header


def test_gmail_link_strips_brackets_and_encodes():
    assert email_sync.gmail_link("<abc+1@mail.acme.com>") == (
        "https://mail.google.com/mail/u/0/#search/"
        "rfc822msgid%3Aabc%2B1%40mail.acme.com"
    )
    assert email_sync.gmail_link(None) is None


# --- pick_application -------------------------------------------------------

def test_pick_application_prefers_title_match_else_newest():
    apps = [
        {"id": 1, "title": "Platform Engineer"},   # newest (fetch order)
        {"id": 2, "title": "Payments Engineer"},
    ]
    chosen, why = email_sync.pick_application(apps, "about the payments engineer role")
    assert chosen["id"] == 2 and why is None
    chosen, why = email_sync.pick_application(apps, "no title mentioned")
    assert chosen["id"] == 1
    assert "assumed newest of 2" in why
    only, why = email_sync.pick_application([apps[0]], "anything")
    assert only["id"] == 1 and why is None


# --- sync over a fake IMAP --------------------------------------------------

class FakeIMAP:
    """Just enough of imaplib: every company search returns all UIDs."""

    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.searches = []

    def select(self, folder, readonly=False):
        self.folder = folder
        return "OK", [b"1"]

    def uid(self, cmd, charset, *args):
        if cmd == "SEARCH":
            self.searches.append(args)
            uids = " ".join(self.messages).encode()
            return "OK", [uids]
        if cmd == "FETCH":
            uid = charset  # imaplib-style: uid("FETCH", uid, parts)
            return "OK", [(b"1 (BODY[] {1})", self.messages[uid]), b")"]
        raise AssertionError(cmd)


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    connection.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    connection.execute(
        """INSERT INTO jobs (id, company_id, external_id, title, gate_failed, dismissed)
           VALUES (10, 1, 'j1', 'Payments Engineer', 0, 0)"""
    )
    connection.commit()
    yield connection
    connection.close()


def app_in_lane(conn, lane, job_id=10):
    app_id = db.create_application(conn, job_id)
    if lane != "considering":
        db.set_application_lane(conn, app_id, lane)
    return app_id


def test_sync_stores_suggestion_with_reason_and_dedupes(conn):
    app_id = app_in_lane(conn, "applied")
    imap = FakeIMAP({"5": raw_email(
        subject="Your application to Acme",
        body="Unfortunately we are not moving forward.",
    )})
    profile = {"email": EMAIL_CFG}

    summary = email_sync.sync(conn, profile, imap=imap)
    assert summary == {"companies": 1, "emails_scanned": 1, "new": 1}
    row = db.fetch_pending_suggestions(conn)[0]
    assert row["application_id"] == app_id
    assert row["suggested_lane"] == "rejected"
    assert row["reason"] == 'matched "not moving forward", "unfortunately"'
    assert row["subject"] == "Your application to Acme"
    assert row["from_addr"] == "r@acme.com"
    # The whole message is kept, plus the Message-ID for a Gmail deep link.
    assert row["body"].strip() == "Unfortunately we are not moving forward."
    assert row["message_id"] == "<abc123@mail.acme.com>"
    # Search asked the server for this company since the lookback date.
    assert any('"Acme"' in arg for arg in imap.searches[0])

    # Second sync sees the same message: no duplicate suggestion.
    assert email_sync.sync(conn, profile, imap=imap)["new"] == 0
    assert len(db.fetch_pending_suggestions(conn)) == 1


def test_sync_unclassified_reply_needs_marker(conn):
    app_in_lane(conn, "applied")
    imap = FakeIMAP({
        "1": raw_email(subject="Acme monthly newsletter", body="Big product news!"),
        "2": raw_email(subject="Thank you for applying to Acme", body="We got it."),
    })
    email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    rows = db.fetch_pending_suggestions(conn)
    assert len(rows) == 1  # the newsletter produced nothing
    assert rows[0]["suggested_lane"] is None
    assert rows[0]["reason"] == 'reply mentions "thank you for applying"'


def test_sync_skips_unwatched_lanes_and_same_lane_suggestions(conn):
    # considering = not applied yet: no application to update, no search.
    app_in_lane(conn, "considering")
    imap = FakeIMAP({"1": raw_email(body="phone screen?")})
    summary = email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert summary == {"companies": 0, "emails_scanned": 0, "new": 0}
    assert imap.searches == []

    # Already interviewing + email suggesting interviewing: nothing to suggest.
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM applications")
    conn.commit()
    app_in_lane(conn, "interviewing")
    summary = email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert summary["emails_scanned"] == 1
    assert summary["new"] == 0


def test_sync_without_credentials_raises(conn):
    app_in_lane(conn, "applied")
    with pytest.raises(ValueError, match="secrets.yaml"):
        email_sync.sync(conn, {"email": EMAIL_CFG})


# --- load_secrets -----------------------------------------------------------

def test_load_secrets_missing_or_incomplete(tmp_path):
    assert email_sync.load_secrets(tmp_path / "none.yaml") is None
    partial = tmp_path / "partial.yaml"
    partial.write_text("gmail:\n  username: me@gmail.com\n")
    assert email_sync.load_secrets(partial) is None
    full = tmp_path / "full.yaml"
    full.write_text("gmail:\n  username: me@gmail.com\n  app_password: 'x y'\n")
    assert email_sync.load_secrets(full) == {"username": "me@gmail.com", "app_password": "x y"}
