"""Tests for inbox → tracker suggestions: triage, message parsing,
application routing, and the sync loop over a fake IMAP."""

from email.message import EmailMessage
from pathlib import Path

import pytest
import yaml

import db
import email_sync

CLASSIFY = {
    "rejected": {
        "strong": ["not (to |be )?mov(e|ing) forward"],
        "weak": ["unfortunately", "at this time"],
    },
    "offer": {"strong": ["offer letter"]},
    "interviewing": {
        "strong": ["technical interview", "phone screen", "schedule a call", "coding challenge"],
        "weak": ["onsite", "hiring manager"],
    },
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
    "ignore_senders": ["newsletter.example"],
    "ignore_phrases": ["verification code", "one[- ]time passcode"],
    "receipt_phrases": ["received your application", "will review"],
    "automated_senders": ["no-?reply", "careers", "^(hello|team)$"],
    "automated_domains": ["ats.example"],
}

MAIL_DATE = "2026-09-10"  # the Date header of raw_email()
AUTOMATED = "no-reply@acme.com"


def mail(subject="", body="", sender=AUTOMATED, bulk=False):
    return {"subject": subject, "body": body, "from_addr": sender, "bulk": bulk}


def decide(subject="", body="", sender=AUTOMATED, bulk=False):
    return email_sync.triage(mail(subject, body, sender, bulk), EMAIL_CFG)


# --- lane buckets -----------------------------------------------------------

def test_ties_keep_bucket_order():
    # Rejection and interviewing score the same — rejected is listed first.
    found = decide("Update", "After your technical interview we are not moving forward.")
    assert found["lane"] == "rejected"
    assert found["reason"] == 'matched "not moving forward" — over interviewing'


def test_best_scoring_bucket_beats_a_stray_phrase():
    # A scheduling email with one incidental "unfortunately" is not a rejection.
    found = decide(
        "Next steps",
        "Unfortunately Thursday is out for the team — "
        "please pick a slot for your technical interview and the onsite.",
    )
    assert found["lane"] == "interviewing"
    assert found["reason"] == 'matched "technical interview", "onsite" (weak) — over rejected'


def test_subject_hits_weigh_extra():
    found = decide(
        "Phone screen with Acme",
        "Unfortunately the recruiter you emailed has left; I'll take over.",
    )
    assert found["lane"] == "interviewing"
    assert found["reason"] == 'matched "Phone screen" (subject) — over rejected'


def test_quoted_thread_history_does_not_vote():
    found = decide(
        "Re: Interview",
        "We are not moving forward.\n\n"
        "On Tue, Sep 22, 2026 at 9:00 AM Recruiter <r@acme.com> wrote:\n"
        "> Let's do your technical interview and onsite\n"
        "> with the hiring manager for a coding challenge.",
    )
    assert found["lane"] == "rejected"
    assert found["reason"] == 'matched "not moving forward"'


def test_one_weak_phrase_never_decides_but_two_do():
    assert decide("Update", "Unfortunately the office is closed.")["lane"] is None
    assert decide("Update", "Unfortunately, not at this time.")["lane"] == "rejected"


def test_phrases_match_whole_words_only():
    # Newsletter prose: "coding challenges" must not read as a coding challenge.
    assert decide("", "Agentic coding challenges the case for components.")["lane"] is None
    assert decide("", "Your coding challenge is ready.")["lane"] == "interviewing"


def test_phrases_survive_line_wraps_and_curly_apostrophes():
    assert decide("", "We have decided\nnot to move\n forward.")["lane"] == "rejected"
    cfg = {**EMAIL_CFG, "classify": {"rejected": {"strong": ["we've decided"]}}}
    assert email_sync.triage(mail("", "We’ve decided."), cfg)["lane"] == "rejected"


def test_reason_says_what_the_mail_said_not_the_pattern():
    found = decide("", "We will not be moving forward.")
    assert found["reason"] == 'matched "not be moving forward"'


def test_evidence_is_the_sentence_that_decided_it():
    found = decide(
        "Update",
        "Thanks for your time. Sadly we are not moving forward. We wish you well.",
    )
    assert found["evidence"] == "Sadly we are not moving forward."


def test_long_evidence_is_clipped_around_the_phrase():
    found = decide("", "word " * 200 + "not moving forward " + "word " * 200)
    assert "not moving forward" in found["evidence"]
    assert len(found["evidence"]) <= email_sync.EVIDENCE_LIMIT + 1


# --- noise ------------------------------------------------------------------

def test_verification_code_is_filtered_despite_mentioning_the_application():
    found = decide(
        "Your verification code",
        "Please confirm using the code below. We look forward to receiving your application.",
    )
    assert found["kind"] == "noise"
    assert found["reason"] == 'ignored: mentions "verification code"'
    assert found["evidence"] is None  # never keep the mail text around a code


def test_ignored_sender_beats_everything():
    found = decide("Acme", "Try this technical interview trick", sender="dan@newsletter.example")
    assert found["kind"] == "noise"
    assert found["reason"] == 'ignored sender "newsletter.example"'


def test_lane_phrase_outranks_ignored_phrase():
    # An assessment invite that hands out a passcode is still an invite.
    found = decide("Next steps", "Your coding challenge one-time passcode is below.")
    assert found["kind"] == "interviewing"


def test_list_mail_mentioning_the_marker_only_in_the_body_is_filtered():
    found = decide("Five tips", "Here is how your application uses a model.", bulk=True)
    assert found["kind"] == "noise"
    assert "mailing list" in found["reason"]


def test_list_mail_with_the_marker_in_the_subject_stays():
    assert decide("Update on your application", "Log in to read it.", bulk=True)["kind"] == "reply"


# --- receipts ---------------------------------------------------------------

def test_receipt_is_filed_without_a_lane():
    found = decide("Thanks from Acme", "We received your application. Hang tight.")
    assert found["kind"] == "receipt"
    assert found["auto_file"] is True
    assert found["lane"] is None
    assert found["evidence"] == "We received your application."


def test_receipt_with_a_stray_signal_stays_visible():
    found = decide("Thanks", "We received your application. Unfortunately, scams are up.")
    assert found["kind"] == "reply"
    assert found["auto_file"] is False
    assert found["reason"] == 'looks like a receipt, but also says "Unfortunately" (weak)'


def test_interview_invite_that_acknowledges_the_application_is_not_a_receipt():
    found = decide("Thanks", "We received your application. Can we schedule a call?")
    assert found["kind"] == "interviewing"


def test_receipt_words_in_the_subject_alone_do_not_file():
    assert decide("We received your application", "Log in for details.")["kind"] == "reply"


# --- people and plain replies ------------------------------------------------

def test_person_surfaces_with_no_phrase_at_all():
    found = decide("Software Engineer at Acme", "Hi Alex, do you have time to talk this week?",
                   sender="jane.doe@acme.com")
    assert found["kind"] == "person"
    assert found["auto_file"] is False
    assert found["evidence"] == "do you have time to talk this week?"


def test_person_is_never_filed_as_a_receipt():
    found = decide("Thanks", "We received your application, let me know a good time.",
                   sender="jane.doe@acme.com")
    assert found["kind"] == "person"


@pytest.mark.parametrize("sender", [
    "no-reply@acme.com", "careers@acme.com", "hello@acme.com", "anyone@ats.example",
    "anyone@mail.ats.example",
])
def test_automated_senders_are_not_people(sender):
    assert not email_sync.is_person(mail(sender=sender), EMAIL_CFG)


def test_list_mail_is_not_a_person_and_blank_sender_is_not_either():
    assert not email_sync.is_person(mail(sender="jane@acme.com", bulk=True), EMAIL_CFG)
    assert not email_sync.is_person(mail(sender=""), EMAIL_CFG)


def test_unclassified_mail_needs_a_marker():
    found = decide("Update on your application", "Log in to read it.")
    assert found["kind"] == "reply"
    assert found["reason"] == 'reply mentions "your application"'
    assert decide("50% off this weekend only", "Big news!")["kind"] is None


def test_strip_quoted_cuts_forward_markers_and_quote_lines():
    body = "New part\n> quoted line\n---------- Forwarded message ----------\nold part"
    assert email_sync.strip_quoted(body) == "New part"


def test_reply_marker_gates_unclassified_mail():
    assert email_sync.reply_marker("Update on your application", MARKERS) == "your application"
    assert email_sync.reply_marker("50% off this weekend only", MARKERS) is None


def test_kind_label_falls_back_to_the_lane_for_old_rows():
    assert email_sync.kind_label("receipt") == "Receipt"
    assert email_sync.kind_label(None, "rejected") == "Rejection"
    assert email_sync.kind_label(None, None) == "Reply"


# --- the wording the real config has to handle --------------------------------

PROFILE_YAML = Path(__file__).parent.parent / "config" / "profile.yaml"
needs_profile = pytest.mark.skipif(
    not PROFILE_YAML.exists(), reason="profile.yaml is private; the mirror has only the example"
)


@pytest.fixture
def real_cfg():
    return yaml.safe_load(PROFILE_YAML.read_text())["email"]


@needs_profile
@pytest.mark.parametrize("body", [
    "After reviewing your application, we've decided not to move forward at this time.",
    "At this time, we have decided to move forward with other candidates.",
    "We are pursuing candidates with skills which more closely match the position.",
    "Unfortunately we just hired someone for this role, so the position is no longer available.",
    "We've reviewed your application and, unfortunately, will not be moving forward.",
    "After careful consideration, we have decided to not move forward with your application.",
])
def test_real_config_reads_rejections(real_cfg, body):
    found = email_sync.triage(mail("Thank you for applying to Acme", body), real_cfg)
    assert found["kind"] == "rejected", found


@needs_profile
@pytest.mark.parametrize("body", [
    "We've received your application and will review it as soon as possible.",
    "Your application has been received and we will review it soon.",
    "Our team will be in touch about next steps if there is a match.",
    "We just got your application for the Backend Engineer role!",
])
def test_real_config_files_receipts(real_cfg, body):
    found = email_sync.triage(mail("Thank you for applying to Acme", body), real_cfg)
    assert found["kind"] == "receipt", found


@needs_profile
@pytest.mark.parametrize("subject, body", [
    ("Your verification code", "Thank you for your interest in Acme. We look forward to "
                               "receiving your application."),
    ("Please confirm your identity", "Enter the one-time passcode to finish your application."),
])
def test_real_config_filters_account_mail(real_cfg, subject, body):
    assert email_sync.triage(mail(subject, body), real_cfg)["kind"] == "noise"


@needs_profile
def test_real_config_filters_a_portal_login_code_from_a_hash_address(real_cfg):
    # Looks like a person to every other check: this has to be filtered.
    found = email_sync.triage(mail(
        "Please verify your login at Acme Careers",
        "Hello Alex. Here is the code required to complete your form on the Acme "
        "careers website.",
        sender="user-7e9dd6a0cc63e3df58ab7fe33038b431@acme.mail.ats.example",
    ), real_cfg)
    assert found["kind"] == "noise", found


@needs_profile
def test_real_config_does_not_take_a_hash_address_for_a_person(real_cfg):
    sender = "user-7e9dd6a0cc63e3df58ab7fe33038b431@acme.example"
    assert not email_sync.is_person(mail(sender=sender), real_cfg)
    assert email_sync.is_person(mail(sender="jane.doe@acme.example"), real_cfg)


@needs_profile
def test_real_config_marketing_link_is_not_a_receipt_signal(real_cfg):
    found = email_sync.triage(mail("Update", "Hear from our CEO about the mission."), real_cfg)
    assert found["kind"] != "receipt", found


@needs_profile
def test_real_config_keeps_an_invite_out_of_the_receipts(real_cfg):
    body = "We received your application and would love to talk. Pick a time on my calendly."
    found = email_sync.triage(mail("Thank you for applying to Acme", body), real_cfg)
    assert found["kind"] == "interviewing", found


# --- parse_message ----------------------------------------------------------

def raw_email(subject="Hello", body="Plain body", html=None, sender="Recruiter <r@acme.com>",
              date="Thu, 10 Sep 2026 12:30:00 -0400", headers=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = "<abc123@mail.acme.com>"
    for name, value in (headers or {}).items():
        msg[name] = value
    if html is not None:
        msg.set_content(body) if body else None
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(body)
    return bytes(msg)


def test_parse_message_plain():
    parsed = email_sync.parse_message("7", raw_email())
    assert parsed["uid"] == "7"
    assert parsed["from_name"] == "Recruiter"
    assert parsed["from_addr"] == "r@acme.com"
    assert parsed["subject"] == "Hello"
    assert parsed["received_at"] == "2026-09-10 16:30:00"  # UTC
    assert parsed["body"].strip() == "Plain body"
    assert parsed["message_id"] == "<abc123@mail.acme.com>"
    assert parsed["bulk"] is False


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


def test_parse_message_drops_inline_css():
    raw = raw_email(body="", html="<style>#outlook a { padding:0; }</style><p>Hi Alex</p>")
    assert email_sync.parse_message("1", raw)["body"] == "Hi Alex"


@pytest.mark.parametrize("headers", [
    {"List-Unsubscribe": "<mailto:u@acme.com>"},
    {"List-Id": "news.acme.com"},
    {"Precedence": "bulk"},
])
def test_parse_message_flags_list_mail(headers):
    assert email_sync.parse_message("1", raw_email(headers=headers))["bulk"] is True


def test_gmail_link_strips_brackets_and_encodes():
    assert email_sync.gmail_link("<abc+1@mail.acme.com>") == (
        "https://mail.google.com/mail/u/0/#search/"
        "rfc822msgid%3Aabc%2B1%40mail.acme.com"
    )
    assert email_sync.gmail_link(None) is None


# --- pick_application -------------------------------------------------------

WATCHED = {"applied", "interviewing", "offer"}


def application(app_id, title, lane="applied", applied_at="2026-09-01"):
    return {"id": app_id, "title": title, "lane": lane, "applied_at": applied_at,
            "created_at": "2026-09-01 12:00:00"}


def pick(apps, text, received_at=None):
    return email_sync.pick_application(apps, text, watched=WATCHED, received_at=received_at)


def test_pick_prefers_the_named_title():
    apps = [application(1, "Platform Engineer"), application(2, "Payments Engineer")]
    chosen, note = pick(apps, "about the payments engineer role")
    assert chosen["id"] == 2 and note is None


def test_pick_title_survives_wrapped_lines():
    apps = [application(1, "Platform Engineer"), application(2, "Software Engineer, Payments")]
    chosen, _ = pick(apps, "the Software Engineer,\r\n Payments position")
    assert chosen["id"] == 2


def test_pick_longest_named_title_wins():
    apps = [application(1, "Engineer II"), application(2, "Software Engineer II, Backend")]
    chosen, note = pick(apps, "the Software Engineer II, Backend role")
    assert chosen["id"] == 2 and note is None


def test_pick_returns_a_closed_application_the_mail_names():
    # The mail is about #1, already rejected: #2 must not inherit it.
    apps = [application(1, "Platform Engineer", lane="rejected"),
            application(2, "Payments Engineer")]
    chosen, _ = pick(apps, "the Platform Engineer role")
    assert chosen["id"] == 1


def test_pick_without_a_title_says_it_guessed():
    apps = [application(1, "Platform Engineer", applied_at="2026-09-02"),
            application(2, "Payments Engineer", applied_at="2026-09-05")]
    chosen, note = pick(apps, "no title mentioned")
    assert chosen["id"] == 2
    assert note == "can't tell which of 2 open applications, put on the newest"


def test_pick_single_application_needs_no_title():
    chosen, note = pick([application(1, "Platform Engineer")], "anything")
    assert chosen["id"] == 1 and note is None


def test_pick_skips_applications_that_did_not_exist_yet():
    apps = [application(1, "Platform Engineer", lane="rejected", applied_at="2026-08-01"),
            application(2, "Payments Engineer", applied_at="2026-09-30")]
    assert pick(apps, "no title", received_at="2026-09-10 12:00:00") == (None, None)


def test_pick_grace_covers_jobs_tracked_after_applying():
    apps = [application(1, "Platform Engineer", applied_at="2026-09-12")]
    chosen, _ = pick(apps, "no title", received_at="2026-09-10 12:00:00")
    assert chosen["id"] == 1


def test_pick_reapplication_prefers_the_open_one():
    apps = [application(1, "Platform Engineer", lane="rejected", applied_at="2026-08-01"),
            application(2, "Platform Engineer", applied_at="2026-09-05")]
    chosen, note = pick(apps, "the Platform Engineer role", received_at="2026-09-10 12:00:00")
    assert chosen["id"] == 2 and note is None


def test_pick_two_open_reapplications_are_flagged():
    apps = [application(1, "Platform Engineer", applied_at="2026-08-01"),
            application(2, "Platform Engineer", applied_at="2026-09-05")]
    chosen, note = pick(apps, "the Platform Engineer role")
    assert chosen["id"] == 2
    assert "can't tell which of 2" in note


def test_pick_with_nothing_open_and_nothing_named():
    assert pick([application(1, "Platform Engineer", lane="rejected")], "no title") == (None, None)


# --- sync over a fake IMAP --------------------------------------------------

class FakeIMAP:
    """Just enough of imaplib: every company search returns all UIDs."""

    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.searches = []
        self.fetched = []
        self.readonly = None

    def select(self, folder, readonly=False):
        self.folder = folder
        self.readonly = readonly
        return "OK", [b"1"]

    def uid(self, cmd, charset, *args):
        if cmd == "SEARCH":
            self.searches.append(args)
            uids = " ".join(self.messages).encode()
            return "OK", [uids]
        if cmd == "FETCH":
            uid = charset  # imaplib-style: uid("FETCH", uid, parts)
            self.fetched.append(uid)
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
           VALUES (10, 1, 'j1', 'Payments Engineer', 0, 0),
                  (11, 1, 'j2', 'Platform Engineer', 0, 0)"""
    )
    connection.commit()
    yield connection
    connection.close()


def app_in_lane(conn, lane, job_id=10):
    """An application that already existed when raw_email()'s mail arrived."""
    app_id = db.create_application(conn, job_id)
    if lane != "considering":
        db.set_application_lane(conn, app_id, lane)
    conn.execute(
        "UPDATE applications SET applied_at = '2026-09-01', created_at = '2026-09-01 12:00:00' "
        "WHERE id = ?", (app_id,),
    )
    conn.commit()
    return app_id


def rows(conn, table="email_suggestions"):
    return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def test_sync_stores_suggestion_with_reason_and_dedupes(conn):
    app_id = app_in_lane(conn, "applied")
    imap = FakeIMAP({"5": raw_email(
        subject="Your application to Acme",
        body="Unfortunately we are not moving forward.",
    )})
    profile = {"email": EMAIL_CFG}

    summary = email_sync.sync(conn, profile, imap=imap)
    assert summary == {"companies": 1, "emails_scanned": 1, "new": 1,
                       "receipts": 0, "ignored": 0}
    row = db.fetch_pending_suggestions(conn)[0]
    assert row["application_id"] == app_id
    assert row["suggested_lane"] == "rejected"
    assert row["kind"] == "rejected"
    assert row["reason"] == 'matched "not moving forward", "Unfortunately" (weak)'
    assert row["evidence"] == "Unfortunately we are not moving forward."
    assert row["subject"] == "Your application to Acme"
    assert row["from_addr"] == "r@acme.com"
    assert row["from_name"] == "Recruiter"
    # The whole message is kept, plus the Message-ID for a Gmail deep link.
    assert row["body"].strip() == "Unfortunately we are not moving forward."
    assert row["message_id"] == "<abc123@mail.acme.com>"
    # Search asked the server for this company since the lookback date.
    assert any('"Acme"' in arg for arg in imap.searches[0])

    # Second sync sees the same message: no duplicate, not even refetched.
    imap.fetched.clear()
    assert email_sync.sync(conn, profile, imap=imap)["new"] == 0
    assert imap.fetched == []
    assert len(db.fetch_pending_suggestions(conn)) == 1


def test_sync_unclassified_reply_needs_marker(conn):
    app_in_lane(conn, "applied")
    imap = FakeIMAP({
        "1": raw_email(subject="Acme monthly newsletter", body="Big product news!",
                       sender="news@acme.com", headers={"List-Id": "news.acme.com"}),
        "2": raw_email(subject="Thank you for applying to Acme", body="We got it.",
                       sender="no-reply@acme.com"),
    })
    email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    found = db.fetch_pending_suggestions(conn)
    assert len(found) == 1  # the newsletter produced nothing
    assert found[0]["suggested_lane"] is None
    assert found[0]["reason"] == 'reply mentions "thank you for applying"'


def test_sync_files_receipts_without_a_card_or_a_lane_move(conn):
    app_id = app_in_lane(conn, "applied")
    imap = FakeIMAP({"1": raw_email(
        subject="Thanks for applying", body="We received your application.",
        sender="Acme <no-reply@acme.com>",
    )})
    summary = email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert summary["receipts"] == 1 and summary["new"] == 0
    assert db.fetch_pending_suggestions(conn) == []
    filed = db.fetch_email_activity(conn)[0]  # still in the reply history
    assert filed["status"] == "accepted"
    assert filed["kind"] == "receipt"
    assert filed["application_id"] == app_id
    assert db.get_application(conn, app_id)["lane"] == "applied"


def test_sync_records_noise_with_its_reason_but_no_body(conn):
    app_in_lane(conn, "applied")
    imap = FakeIMAP({"1": raw_email(
        subject="Your verification code", body="Code 123456. Finish your application.",
        sender="no-reply@acme.com",
    )})
    summary = email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert summary["ignored"] == 1 and summary["new"] == 0
    assert rows(conn) == []
    ignored = db.fetch_ignored_emails(conn)[0]
    assert ignored["reason"] == 'ignored: mentions "verification code"'
    assert ignored["company"] == "Acme"
    assert "body" not in ignored.keys()
    assert db.count_ignored_emails(conn) == 1

    # Known mail: not fetched or counted again.
    imap.fetched.clear()
    assert email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)["ignored"] == 0
    assert imap.fetched == []


def test_sync_masks_a_code_in_the_subject_of_ignored_mail(conn):
    app_in_lane(conn, "applied")
    imap = FakeIMAP({"1": raw_email(
        subject="944552 - Your Acme verification code", sender="no-reply@acme.com",
    )})
    email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert db.fetch_ignored_emails(conn)[0]["subject"] == "•••• - Your Acme verification code"
    assert "944552" not in email_sync.format_dry_run(
        email_sync.dry_run(conn, {"email": EMAIL_CFG}, imap)
    )


def test_sync_surfaces_a_person_who_uses_no_phrase(conn):
    app_in_lane(conn, "applied")
    imap = FakeIMAP({"1": raw_email(
        subject="Acme role", body="Hi Alex, free to talk this week?",
        sender="Jane Doe <jane.doe@acme.com>",
    )})
    email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    row = db.fetch_pending_suggestions(conn)[0]
    assert row["kind"] == "person"
    assert row["suggested_lane"] is None
    assert row["from_name"] == "Jane Doe"


def test_mail_never_moves_to_the_next_application_when_one_closes(conn):
    # The bug this guards: rejection lands on A, A is accepted to Rejected,
    # A leaves the watched lanes, and the next sync handed the same mail
    # to B, rejecting it too.
    first = app_in_lane(conn, "applied", job_id=10)
    second = app_in_lane(conn, "applied", job_id=11)
    imap = FakeIMAP({"9": raw_email(
        subject="Update", body="About the Payments Engineer role: we are not moving forward.",
    )})
    email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    db.set_application_lane(conn, first, "rejected")
    db.set_suggestion_status(conn, rows(conn)[0]["id"], "accepted")

    assert email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)["new"] == 0
    assert [row["application_id"] for row in rows(conn)] == [first]
    assert db.get_application(conn, second)["lane"] == "applied"


def test_mail_naming_a_closed_application_lands_nowhere(conn):
    # First sight of the mail, its application already closed by hand.
    app_in_lane(conn, "rejected", job_id=10)
    app_in_lane(conn, "applied", job_id=11)
    imap = FakeIMAP({"9": raw_email(
        subject="Update", body="About the Payments Engineer role: we are not moving forward.",
    )})
    assert email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)["new"] == 0
    assert rows(conn) == []


def test_mail_older_than_the_application_lands_nowhere(conn):
    app_id = app_in_lane(conn, "applied")
    conn.execute("UPDATE applications SET applied_at = '2026-10-01' WHERE id = ?", (app_id,))
    conn.commit()
    imap = FakeIMAP({"9": raw_email(body="We are not moving forward.")})
    assert email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)["new"] == 0


def test_a_guess_between_applications_is_written_into_the_reason(conn):
    app_in_lane(conn, "applied", job_id=10)
    app_in_lane(conn, "applied", job_id=11)
    imap = FakeIMAP({"9": raw_email(body="We are not moving forward.")})
    email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert "can't tell which of 2 open applications" in rows(conn)[0]["reason"]


def test_company_spellings_count_as_one_company(conn):
    conn.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (2, 'acme', 'lever', 'acme2')"
    )
    conn.execute(
        """INSERT INTO jobs (id, company_id, external_id, title, gate_failed, dismissed)
           VALUES (12, 2, 'j3', 'Data Engineer', 0, 0)"""
    )
    conn.commit()
    app_in_lane(conn, "applied", job_id=10)
    wanted = app_in_lane(conn, "applied", job_id=12)
    imap = FakeIMAP({"9": raw_email(body="The Data Engineer role: we are not moving forward.")})
    summary = email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert summary["companies"] == 1
    assert [row["application_id"] for row in rows(conn)] == [wanted]


def test_sync_skips_unwatched_lanes_and_same_lane_suggestions(conn):
    # considering = not applied yet: no application to update, no search.
    app_in_lane(conn, "considering")
    imap = FakeIMAP({"1": raw_email(body="phone screen?")})
    summary = email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert summary["companies"] == 0 and summary["emails_scanned"] == 0
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


def test_format_summary_only_mentions_what_happened():
    base = {"companies": 2, "emails_scanned": 5, "new": 1, "receipts": 0, "ignored": 0}
    assert email_sync.format_summary(base) == (
        "Checked mail for 2 companies: 5 emails scanned, 1 new suggestion."
    )
    assert email_sync.format_summary({**base, "receipts": 2, "ignored": 1}) == (
        "Checked mail for 2 companies: 5 emails scanned, 1 new suggestion, "
        "2 receipts filed, 1 ignored."
    )


# --- dry run ----------------------------------------------------------------

def test_dry_run_stores_nothing_and_reports_every_outcome(conn):
    app_in_lane(conn, "applied", job_id=10)
    app_in_lane(conn, "rejected", job_id=11)
    imap = FakeIMAP({
        "1": raw_email(subject="Your verification code", sender="no-reply@acme.com"),
        "2": raw_email(subject="Thanks", body="We received your application.",
                       sender="no-reply@acme.com"),
        "3": raw_email(subject="Sale", body="50% off", sender="no-reply@acme.com"),
        "4": raw_email(subject="Update", sender="no-reply@acme.com",
                       body="The Platform Engineer role: we are not moving forward."),
        "5": raw_email(subject="Update", sender="no-reply@acme.com",
                       body="The Payments Engineer role: we are not moving forward."),
    })
    found = {row["subject"] + row["outcome"][:3]: row
             for row in email_sync.dry_run(conn, {"email": EMAIL_CFG}, imap)}
    assert imap.readonly is True
    assert {row["outcome"] for row in found.values()} == {
        "ignore", "file: Payments Engineer", "drop", "skip", "suggest: Payments Engineer",
    }
    assert "which is rejected" in found["Updateski"]["reason"]
    assert rows(conn) == [] and rows(conn, "email_ignored") == []

    text = email_sync.format_dry_run(list(found.values()))
    assert "5 emails, dry run: nothing was stored." in text
    assert "[Rejection] suggest: Payments Engineer" in text


def test_dry_run_shows_mail_sync_already_recorded(conn):
    app_in_lane(conn, "applied")
    imap = FakeIMAP({"1": raw_email(body="We are not moving forward.")})
    email_sync.sync(conn, {"email": EMAIL_CFG}, imap=imap)
    assert len(email_sync.dry_run(conn, {"email": EMAIL_CFG}, imap)) == 1


def test_dry_run_days_overrides_the_lookback(conn):
    from datetime import datetime, timezone

    app_in_lane(conn, "applied")
    imap = FakeIMAP({})
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    email_sync.dry_run(conn, {"email": EMAIL_CFG}, imap, days=60, now=now)
    assert "06-Sep-2026" in imap.searches[0]


# --- load_secrets -----------------------------------------------------------

def test_load_secrets_missing_or_incomplete(tmp_path):
    assert email_sync.load_secrets(tmp_path / "none.yaml") is None
    partial = tmp_path / "partial.yaml"
    partial.write_text("gmail:\n  username: me@gmail.com\n")
    assert email_sync.load_secrets(partial) is None
    full = tmp_path / "full.yaml"
    full.write_text("gmail:\n  username: me@gmail.com\n  app_password: 'x y'\n")
    assert email_sync.load_secrets(full) == {"username": "me@gmail.com", "app_password": "x y"}
