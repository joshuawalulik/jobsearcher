"""Tests for contacts' mail → the interaction log, over a fake IMAP."""

from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

import autosweep
import contact_sync
import db
import email_sync

NOW = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
PROFILE = {
    "email": {"imap_host": "imap.example.com"},
    "contact_sync": {
        "enabled": True,
        "folder": "ALL",
        "lookback_days": 7,
        "alert_senders": ["alerts@linkedin.example"],
        "digest_senders": ["digest@linkedin.example"],
        "lead_priority": "C",
        "ignore_names": ["Sam Spam"],
        "ignore_phrases": ["money back"],
        "locations": [{"label": "NY", "words": ["Brooklyn"]}],
    },
    "outreach": {
        "company_suffixes": [],
        "company_aliases": [],
        "dont_double_up": [],
        "do_not_contact": [{"company": "Umbrella", "email_domains": ["umbrella.example"],
                            "reason": "former employer"}],
    },
}
THREAD = "https://www.linkedin.com/messaging/thread/2-abc==/"
FOOTER = "\n----------------------------------------\n\nThis email was intended for Alex"


def alert(name, text, subject="A role", msg_id="<1@x>", date="Mon, 09 Mar 2026 15:00:00 +0000"):
    body = f"{subject}\n\n{name}\nReply\n{THREAD}\n\n{text}{FOOTER}"
    return raw(f'"{name}" <alerts@linkedin.example>', subject, body, msg_id, date)


def raw(sender, subject, body, msg_id="<1@x>", date="Mon, 09 Mar 2026 15:00:00 +0000",
        to="alex@example.com"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Message-ID"], msg["Date"] = msg_id, date
    msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    """Answers each X-GM-RAW search by the sender or folder it names."""

    def __init__(self, **by_source):
        self.by_source = by_source
        self.messages = {}
        self.queries = []
        self.readonly = None
        for source, raws in by_source.items():
            for index, data in enumerate(raws):
                self.messages[f"{source}{index}"] = data

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def uid(self, cmd, charset, *args):
        if cmd == "SEARCH":
            query = args[1].strip(chr(34))
            self.queries.append(query)
            source = (
                "alert" if "alerts@" in query else "digest" if "digest@" in query
                else "out" if "in:sent" in query else "in"
            )
            found = [uid for uid in self.messages if uid.startswith(source)]
            return "OK", [" ".join(found).encode()]
        return "OK", [(b"1 (BODY[] {1})", self.messages[charset]), b")"]


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "conversations"


def contact(conn, **overrides):
    fields = {"name": "Pat Doe", "company": "Acme", "track": "agency",
              "source": "own_pipeline", "status": "Replied", **overrides}
    return db.insert_contact(conn, **fields)


def run(conn, imap, folder, **kwargs):
    return contact_sync.sync(conn, PROFILE, imap=imap, now=NOW, folder=folder, **kwargs)


def log_of(conn, contact_id):
    return db.fetch_interactions(conn, contact_id)


def test_linkedin_message_is_the_text_between_link_and_footer():
    body = f"Subject\n\nPat\nReply\n{THREAD}\n\nHello there.\n\nPat{FOOTER}"
    found = contact_sync.linkedin_message(body)
    assert (found["text"], found["thread"]) == ("Hello there.\n\nPat", THREAD)


FIRST_TOUCH = """A role

Sam Roe
Recruiter at Globex Staffing | Hiring engineers
https://www.linkedin.com/comm/in/sam-roe?trk=mail
Hi Alex, a role in Brooklyn.

Worth a chat?

Yes, interested…https://www.linkedin.com/comm/messaging/thread/2-abc==/?a=1
No thanks…https://www.linkedin.com/comm/messaging/thread/2-abc==/?a=2
Reply
        https://www.linkedin.com/comm/messaging/thread/2-abc==/?a=3
"""


def test_the_first_touch_layout_is_read_too():
    found = contact_sync.linkedin_message(FIRST_TOUCH)
    assert found["text"] == "Hi Alex, a role in Brooklyn.\n\nWorth a chat?"
    assert found["thread"] == THREAD
    assert found["profile"] == "https://www.linkedin.com/in/sam-roe/"
    assert found["headline"].startswith("Recruiter at Globex")


def test_a_first_touch_lead_keeps_their_headline_and_profile(conn, folder):
    mail = raw('"Sam Roe" <alerts@linkedin.example>', "A role", FIRST_TOUCH)
    run(conn, FakeIMAP(alert=[mail]), folder)
    row = db.fetch_contacts(conn)[0]
    assert row["company"] == "Globex Staffing"
    assert row["linkedin_url"] == "https://www.linkedin.com/in/sam-roe/"


def test_a_wrapped_subject_reads_as_one_line(conn, folder):
    contact_id = contact(conn)
    mail = alert("Pat Doe", "Hello.", subject="A long subject line that keeps on going " * 3)
    run(conn, FakeIMAP(alert=[mail]), folder)
    assert "\n" not in log_of(conn, contact_id)[0]["summary"]


def test_a_known_contact_gets_logged_flagged_and_saved(conn, folder):
    contact_id = contact(conn, next_step="Wait for the JD", last_reply="2026-03-01")
    summary = run(conn, FakeIMAP(alert=[alert("Pat Doe", "Here is the JD.")]), folder)
    assert summary["logged"] == 1
    entry = log_of(conn, contact_id)[0]
    assert (entry["date"], entry["channel"], entry["direction"]) == ("2026-03-09", "linkedin", "in")
    assert "Here is the JD." in entry["summary"]
    row = db.get_contact(conn, contact_id)
    assert row["new_message"] == "2026-03-09"
    assert row["next_step"] == "Wait for the JD"
    saved = (folder / "pat-doe.md").read_text()
    assert "Here is the JD." in saved and THREAD in saved
    assert "data/conversations/pat-doe.md" in row["notes"]


def test_a_stranger_becomes_a_lead_without_counting_as_a_reply(conn, folder):
    text = "A role in Brooklyn.\n\nSam Roe\nRecruiter at Globex Staffing"
    summary = run(conn, FakeIMAP(alert=[alert("Sam Roe", text)]), folder)
    assert summary["leads"] == 1
    row = db.fetch_contacts(conn)[0]
    assert (row["status"], row["source"], row["priority"]) == ("Not started", "linkedin", "C")
    assert (row["company"], row["location"]) == ("Globex Staffing", "NY")
    assert row["last_reply"] is None
    assert row["new_message"] == "2026-03-09"
    assert (folder / "leads" / "sam-roe.md").exists()
    assert "data/conversations/leads/sam-roe.md" in row["notes"]


def test_two_alerts_from_a_new_person_make_one_contact(conn, folder):
    mail = [alert("Sam Roe", "First."), alert("Sam Roe", "Second.", msg_id="<2@x>")]
    run(conn, FakeIMAP(alert=mail), folder)
    rows = db.fetch_contacts(conn)
    assert len(rows) == 1
    assert len(log_of(conn, rows[0]["id"])) == 2


def test_each_message_is_only_handled_once(conn, folder):
    contact_id = contact(conn)
    imap = FakeIMAP(alert=[alert("Pat Doe", "Hello.")])
    run(conn, imap, folder)
    assert run(conn, imap, folder)["logged"] == 0
    assert len(log_of(conn, contact_id)) == 1


def test_services_never_become_leads(conn, folder):
    mail = [alert("Sam Spam", "Hello."), alert("Jo Roe", "Or your money back!", msg_id="<2@x>")]
    summary = run(conn, FakeIMAP(alert=mail), folder)
    assert db.fetch_contacts(conn) == []
    assert len(summary["ignored"]) == 2


def test_a_removed_contact_is_not_brought_back(conn, folder):
    contact_id = contact(conn)
    db.remove_contact(conn, contact_id)
    summary = run(conn, FakeIMAP(alert=[alert("Pat Doe", "Hello.")]), folder)
    assert db.fetch_contacts(conn) == []
    assert log_of(conn, contact_id) == []
    assert "removed" in summary["ignored"][0]


def test_a_name_matching_two_contacts_is_left_alone(conn, folder):
    contact(conn, name="Pat Doe")
    contact(conn, name="Pat Dunn")
    summary = run(conn, FakeIMAP(alert=[alert("Pat D.", "Hello.")]), folder)
    assert len(db.fetch_contacts(conn)) == 2
    assert "more than one" in summary["ignored"][0]


def test_digests_count_for_contacts_and_leave_strangers_alone(conn, folder):
    contact_id = contact(conn)
    mail = [
        raw('"Pat Doe via LinkedIn" <digest@linkedin.example>', "New message",
            f"You have 1 new message\nRead message: {THREAD}\n"),
        raw('"A Friend via LinkedIn" <digest@linkedin.example>', "New message",
            f"Read message: {THREAD}\n", msg_id="<2@x>"),
    ]
    run(conn, FakeIMAP(digest=mail), folder)
    assert len(db.fetch_contacts(conn)) == 1
    assert "text not in the notification" in log_of(conn, contact_id)[0]["summary"]
    assert [p.name for p in folder.iterdir()] == ["pat-doe.md"]


def test_direct_mail_matches_the_exact_address_and_drops_quoted_history(conn, folder):
    contact_id = contact(conn, email="Pat@acme.example")
    body = "Thanks for the chat.\n\nOn Mon, Pat wrote:\n> old text\n"
    run(conn, FakeIMAP(**{"in": [raw("Pat <pat@acme.example>", "Re: Role", body)]}), folder)
    entry = log_of(conn, contact_id)[0]
    assert (entry["channel"], entry["direction"]) == ("email", "in")
    assert "old text" not in (folder / "pat-doe.md").read_text()


def test_sent_mail_moves_a_draft_to_sent_on_its_real_date(conn, folder):
    contact_id = contact(conn, status="Drafted", email="pat@acme.example", next_step="Send it")
    mail = raw("Alex <alex@example.com>", "Hello", "Hi Pat.", to="pat@acme.example")
    run(conn, FakeIMAP(out=[mail]), folder)
    row = db.get_contact(conn, contact_id)
    assert (row["status"], row["date_contacted"]) == ("Sent", "2026-03-09")
    assert log_of(conn, contact_id)[0]["direction"] == "out"


def test_sent_mail_is_only_read_from_the_sent_folder(conn, folder):
    contact(conn, email="pat@acme.example")
    imap = FakeIMAP()
    run(conn, imap, folder)
    assert any(q.startswith("in:sent to:(pat@acme.example)") for q in imap.queries)
    assert not any(q.startswith("to:") for q in imap.queries)


def test_replying_by_email_clears_the_flag(conn, folder):
    contact_id = contact(conn, email="pat@acme.example", new_message="2026-03-08")
    mail = raw("Alex <alex@example.com>", "Re: Role", "Sounds good.", to="pat@acme.example")
    run(conn, FakeIMAP(out=[mail]), folder)
    assert db.get_contact(conn, contact_id)["new_message"] is None


def test_mail_to_a_do_not_contact_person_is_refused_but_inbound_logs(conn, folder):
    contact_id = contact(conn, company="Umbrella", status="Skip", priority="skip",
                         email="sam@umbrella.example")
    out = raw("Alex <alex@example.com>", "Hi", "Hi.", to="sam@umbrella.example")
    inbound = raw("Sam <sam@umbrella.example>", "Hi", "Hello.", msg_id="<2@x>",
                  date="Tue, 10 Mar 2026 09:00:00 +0000")
    summary = run(conn, FakeIMAP(out=[out], **{"in": [inbound]}), folder)
    assert len(summary["refused"]) == 1
    assert [e["direction"] for e in log_of(conn, contact_id)] == ["in"]
    assert db.get_contact(conn, contact_id)["new_message"] is None


def test_messages_are_applied_oldest_first(conn, folder):
    contact_id = contact(conn)
    mail = [
        alert("Pat Doe", "Later.", msg_id="<2@x>", date="Mon, 09 Mar 2026 18:00:00 +0000"),
        alert("Pat Doe", "Earlier.", msg_id="<1@x>", date="Mon, 09 Mar 2026 09:00:00 +0000"),
    ]
    run(conn, FakeIMAP(alert=mail), folder)
    assert "Earlier." in log_of(conn, contact_id)[0]["summary"]


def test_baseline_marks_mail_seen_without_logging(conn, folder):
    contact_id = contact(conn)
    imap = FakeIMAP(alert=[alert("Pat Doe", "Old news.")])
    assert run(conn, imap, folder, baseline=True)["baseline"] == 1
    assert run(conn, imap, folder)["logged"] == 0
    assert log_of(conn, contact_id) == []
    assert not folder.exists()


def test_the_mailbox_is_opened_read_only(conn, folder):
    imap = FakeIMAP()
    run(conn, imap, folder)
    assert imap.readonly is True


def test_dry_run_writes_nothing(conn, folder):
    contact_id = contact(conn)
    imap = FakeIMAP(alert=[alert("Pat Doe", "Hello."), alert("Sam Roe", "Hi.", msg_id="<2@x>")])
    lines = contact_sync.dry_run(conn, PROFILE, imap, now=NOW)
    assert len(lines) == 2
    assert "ADD_LEAD" in lines[1]
    assert log_of(conn, contact_id) == []
    assert db.seen_contact_mail(conn) == set()


def test_format_summary_shows_what_happened():
    summary = {"logged": 2, "leads": 1, "ignored": ["Sam: ignored name"], "refused": [], "baseline": 0}
    assert contact_sync.format_summary(summary) == (
        "Contact mail: 2 logged, 1 new leads, 1 ignored (Sam: ignored name)."
    )


def test_a_failure_never_stops_the_sweep(conn, monkeypatch, capsys):
    monkeypatch.setattr(email_sync, "load_secrets", lambda: {"username": "a", "app_password": "b"})

    def boom(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(contact_sync, "sync", boom)
    autosweep._contact_sync(conn, PROFILE)
    assert "Contact mail sync failed: no network" in capsys.readouterr().out


def test_sweep_skips_it_when_disabled(conn, monkeypatch):
    monkeypatch.setattr(email_sync, "load_secrets", lambda: {"username": "a", "app_password": "b"})
    monkeypatch.setattr(contact_sync, "sync", lambda *a, **k: pytest.fail("should not run"))
    autosweep._contact_sync(conn, {"contact_sync": {"enabled": False}})
