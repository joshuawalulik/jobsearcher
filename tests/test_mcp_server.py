import asyncio
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


OUTREACH_CFG = {
    "default_ask": "Who handles engineering hiring?",
    "follow_up": {"first_business_days": 5, "second_business_days": 12},
    "weekly_targets": {
        "agency_first_touches": 5,
        "company_recruiter_emails_min": 5,
        "company_recruiter_emails_max": 8,
        "referral_touches": 1,
    },
    "company_suffixes": ["Inc", "Group"],
    "company_aliases": [["Example", "Example Holdings"]],
    "dont_double_up": [{"company": "Initrode", "covered_by": "Pat Doe"}],
    "do_not_contact": [{"company": "Umbrella", "email_domains": ["umbrella.example"],
                        "reason": "former employer"}],
    "referral_watch": {"levels": ["mid"], "title_keywords": ["software"]},
    "templates": {
        "company_recruiter": {"subject": "Hello", "body": "Hi {first}, about {company}."},
        "agency": {"subject": "Searches", "body": "Hi {first}."},
        "alumni": {"subject": "Alum", "body": "Hi {first}."},
        "follow_up": {"subject": "", "body": "Hi {first}. {new_detail}"},
    },
}


@pytest.fixture
def outreach_db(test_db, monkeypatch):
    monkeypatch.setattr(mcp_server, "_outreach_cfg", lambda: OUTREACH_CFG)
    monkeypatch.setattr(mcp_server, "_today", lambda: "2026-03-02")
    return test_db


def _contact(conn, **overrides):
    fields = {"name": "Pat Doe", "company": "Acme", "track": "company_recruiter",
              "source": "peer_list", "priority": "C", **overrides}
    return db.insert_contact(conn, **fields)


def _blocked(conn):
    return _contact(conn, company="Umbrella", email="sam@umbrella.example",
                    status="Skip", priority="skip", peer_history="Replied once")


def test_list_contacts_filters_and_hides_skip(outreach_db):
    a = _contact(outreach_db, priority="A", peer_history="Long history")
    b = _contact(outreach_db, name="Sam Roe", status="Skip", priority="skip")
    rows = mcp_server.list_contacts()
    assert [r["contact_id"] for r in rows] == [a]
    assert "peer_history" not in rows[0]
    assert [r["contact_id"] for r in mcp_server.list_contacts(status="Skip")] == [b]
    assert mcp_server.list_contacts(track="agency") == []
    assert len(mcp_server.list_contacts(company="acme")) == 1


def test_get_contact_has_history_log_and_template(outreach_db):
    contact_id = _contact(outreach_db, peer_history="Replied once")
    db.add_interaction(outreach_db, contact_id, "2026-03-01", "email", "out", "Hello")
    contact = mcp_server.get_contact(contact_id)
    assert contact["peer_history"] == "Replied once"
    assert contact["contact_id"] == contact_id
    assert [i["summary"] for i in contact["interactions"]] == ["Hello"]
    assert contact["suggested_template"]["body"] == "Hi Pat, about Acme."


def test_get_contact_unknown(outreach_db):
    with pytest.raises(ValueError):
        mcp_server.get_contact(999)


def test_add_contact(outreach_db):
    result = mcp_server.add_contact(track="agency", company="Globex")
    assert result["added"] is True
    assert result["warnings"] == []
    row = db.get_contact(outreach_db, result["contact"]["contact_id"])
    assert row["name"] is None
    assert row["status"] == "Not started"


def test_location_is_set_on_add_and_filters_the_list(outreach_db):
    mcp_server.add_contact(track="agency", name="Sam Roe", location="AZ")
    mcp_server.add_contact(track="agency", name="Pat Roe", location="NY")
    found = mcp_server.list_contacts(location="az")
    assert [c["name"] for c in found] == ["Sam Roe"]
    assert found[0]["location"] == "AZ"


def test_add_contact_duplicate_email_returns_the_existing_one(outreach_db):
    contact_id = _contact(outreach_db, email="pat@acme.example")
    result = mcp_server.add_contact(track="agency", name="P", email="PAT@acme.example")
    assert result["added"] is False
    assert result["contact"]["contact_id"] == contact_id
    assert len(db.fetch_contacts(outreach_db)) == 1


def test_add_contact_warns_on_double_up(outreach_db):
    result = mcp_server.add_contact(
        track="company_recruiter", name="Sam Roe", company="Initrode"
    )
    assert result["added"] is True
    assert "already covered" in result["warnings"][0]


def test_add_blocked_contact_is_saved_as_skip(outreach_db):
    result = mcp_server.add_contact(track="agency", name="Sam Roe", company="Umbrella")
    assert result["contact"]["status"] == "Skip"
    assert result["contact"]["do_not_contact"]
    assert "Saved as Skip" in result["warnings"][0]


def test_update_contact_sent_starts_the_clock(outreach_db):
    contact_id = _contact(outreach_db, status="Drafted")
    result = mcp_server.update_contact(contact_id, {"status": "Sent"})
    assert result["contact"]["date_contacted"] == "2026-03-02"
    assert result["contact"]["follow_up_due"] == "2026-03-09"


def test_update_contact_draft_id_moves_to_drafted(outreach_db):
    contact_id = _contact(outreach_db)
    result = mcp_server.update_contact(contact_id, {"gmail_draft_id": "d1"})
    assert result["contact"]["status"] == "Drafted"
    assert result["contact"]["has_gmail_draft"] is True


def test_update_contact_notes_append(outreach_db):
    contact_id = _contact(outreach_db, notes="First")
    mcp_server.update_contact(contact_id, {"notes": "Second"})
    assert db.get_contact(outreach_db, contact_id)["notes"] == "First\n\nSecond"


def test_update_contact_refuses_an_email_already_in_use(outreach_db):
    _contact(outreach_db, email="pat@acme.example")
    other = _contact(outreach_db, name="Sam Roe")
    with pytest.raises(ValueError, match="already used"):
        mcp_server.update_contact(other, {"email": "pat@acme.example"})


def test_update_contact_unknown(outreach_db):
    with pytest.raises(ValueError):
        mcp_server.update_contact(999, {"notes": "x"})


def test_restore_contact(outreach_db):
    contact_id = _contact(outreach_db, status="Skip", priority="skip",
                          notes="Not a recruiter")
    result = mcp_server.restore_contact(contact_id)
    assert result["contact"]["status"] == "Not started"
    assert result["contact"]["priority"] == "C"
    row = db.get_contact(outreach_db, contact_id)
    assert row["notes"] == "Not a recruiter"
    assert row["ask"] == OUTREACH_CFG["default_ask"]


def test_do_not_contact_is_refused_everywhere(outreach_db):
    contact_id = _blocked(outreach_db)
    with pytest.raises(ValueError, match="Do not contact"):
        mcp_server.update_contact(contact_id, {"status": "Sent"})
    with pytest.raises(ValueError, match="Do not contact"):
        mcp_server.restore_contact(contact_id)
    with pytest.raises(ValueError, match="Do not contact"):
        mcp_server.log_interaction(contact_id, "email", "out", "Hello")
    assert db.fetch_interactions(outreach_db, contact_id) == []
    assert db.get_contact(outreach_db, contact_id)["status"] == "Skip"


def test_do_not_contact_still_logs_inbound(outreach_db):
    contact_id = _blocked(outreach_db)
    result = mcp_server.log_interaction(contact_id, "email", "in", "They wrote first")
    assert result["contact"]["status"] == "Skip"
    assert result["contact"]["last_reply"] == "2026-03-02"
    assert len(db.fetch_interactions(outreach_db, contact_id)) == 1


def test_log_interaction_inbound_moves_to_replied(outreach_db):
    contact_id = _contact(outreach_db, status="Sent", date_contacted="2026-02-20")
    result = mcp_server.log_interaction(
        contact_id, "linkedin", "in", "Happy to chat", date="2026-02-25"
    )
    assert result["contact"]["status"] == "Replied"
    assert result["contact"]["last_reply"] == "2026-02-25"


def test_log_interaction_outbound_advances_the_sequence(outreach_db):
    contact_id = _contact(outreach_db)
    result = mcp_server.log_interaction(contact_id, "linkedin", "out", "Connection note")
    assert result["contact"]["status"] == "Sent"
    assert result["contact"]["date_contacted"] == "2026-03-02"


@pytest.mark.parametrize(
    "args",
    [
        ("fax", "out", "Hello", None),
        ("email", "sideways", "Hello", None),
        ("email", "out", "   ", None),
        ("email", "out", "Hello", "soon"),
    ],
)
def test_log_interaction_rejects_bad_input(outreach_db, args):
    contact_id = _contact(outreach_db)
    channel, direction, summary, date = args
    with pytest.raises(ValueError):
        mcp_server.log_interaction(contact_id, channel, direction, summary, date)
    assert db.fetch_interactions(outreach_db, contact_id) == []


def test_list_followups_due(outreach_db):
    due = _contact(outreach_db, status="Sent", date_contacted="2026-02-02")
    _contact(outreach_db, name="Sam Roe", status="Sent", date_contacted="2026-03-01")
    rows = mcp_server.list_followups_due()
    assert [r["contact_id"] for r in rows] == [due]
    assert rows[0]["needs_action"].startswith("First follow-up due")
    assert mcp_server.list_followups_due(as_of="2026-01-01") == []


def test_contacts_for_job_matches_by_company_alias(outreach_db):
    app_id = _make_tracked_job(outreach_db)
    _contact(outreach_db, company="Example Holdings", priority="A")
    result = mcp_server.contacts_for_job(app_id)
    assert result["app_id"] == app_id
    assert result["referral_path"] is False
    assert len(result["contacts_by_track"]["company_recruiter"]) == 1


def test_contacts_for_job_flags_a_real_connection(outreach_db):
    app_id = _make_tracked_job(outreach_db)
    _contact(outreach_db, company="Example", track="referral", source="own_pipeline")
    assert mcp_server.contacts_for_job(app_id)["referral_path_contacts"] == ["Pat Doe"]


def test_list_referral_watch(outreach_db, monkeypatch):
    monkeypatch.setattr(mcp_server, "_years_cfg", lambda: {"junior_max": 1, "senior_min": 5})
    outreach_db.execute(
        "INSERT INTO companies (name, ats_type, slug) VALUES ('Example', 'greenhouse', 'example')"
    )
    job_id = outreach_db.execute(
        """
        INSERT INTO jobs (company_id, external_id, title, description, gate_failed)
        VALUES (1, 'watch-1', 'Software Engineer', '3+ years of experience', 0)
        """
    ).lastrowid
    outreach_db.commit()
    contact_id = _contact(outreach_db, company="Example", track="referral",
                          source="own_pipeline")
    assert mcp_server.list_referral_watch() == []
    result = mcp_server.update_contact(contact_id, {"standing_referral": True})
    assert result["contact"]["standing_referral"] is True
    [match] = mcp_server.list_referral_watch()
    assert (match["job_id"], match["contact_name"]) == (job_id, "Pat Doe")


def test_contacts_for_job_unknown(outreach_db):
    with pytest.raises(ValueError):
        mcp_server.contacts_for_job(999)


def test_link_contact_to_job_adds_them_to_the_results(outreach_db):
    app_id = _make_tracked_job(outreach_db)
    contact_id = _contact(outreach_db, company="Staffing Firm", track="agency",
                          source="own_pipeline")
    assert mcp_server.contacts_for_job(app_id)["contacts_by_track"] == {}
    mcp_server.link_contact_to_job(contact_id, app_id, "Submitted to this req")
    assert mcp_server.contacts_for_job(app_id)["referral_path"] is True


def test_link_contact_to_job_unknown_ids(outreach_db):
    app_id = _make_tracked_job(outreach_db)
    with pytest.raises(ValueError):
        mcp_server.link_contact_to_job(999, app_id)
    with pytest.raises(ValueError):
        mcp_server.link_contact_to_job(_contact(outreach_db), 999)


def test_weekly_summary(outreach_db):
    _contact(outreach_db, status="Sent", date_contacted="2026-03-02")
    summary = mcp_server.weekly_summary()
    assert summary["week_start"] == "2026-03-02"
    assert summary["by_status"] == {"Sent": 1}
    assert summary["progress"]["company_recruiter_emails"]["done"] == 1


@pytest.mark.parametrize(
    "tool, args, reason",
    [
        ("update_contact", {"fields": {"status": "Sent"}}, "Do not contact: former employer"),
        ("restore_contact", {}, "Do not contact: former employer"),
        ("log_interaction",
         {"channel": "email", "direction": "out", "summary": "Hello"},
         "Do not contact: former employer"),
    ],
)
def test_refusal_reasons_reach_the_client(outreach_db, tool, args, reason):
    contact_id = _blocked(outreach_db)
    with pytest.raises(Exception) as caught:
        asyncio.run(mcp_server.mcp.call_tool(tool, {"contact_id": contact_id, **args}))
    assert reason in str(caught.value)


def test_input_mistakes_reach_the_client_too(outreach_db):
    with pytest.raises(Exception) as caught:
        asyncio.run(mcp_server.mcp.call_tool("get_contact", {"contact_id": 999}))
    assert "no contact with contact_id 999" in str(caught.value)


def test_all_tools_are_exposed_and_existing_ones_remain():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in getattr(tools, "tools", tools)}
    assert names == {
        "list_considering_jobs", "get_job", "save_application_note",
        "list_contacts", "get_contact", "add_contact", "update_contact",
        "restore_contact", "log_interaction", "list_followups_due",
        "contacts_for_job", "link_contact_to_job", "weekly_summary",
        "list_referral_watch", "today_plan", "log_block",
    }
