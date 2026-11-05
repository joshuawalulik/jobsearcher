"""Route tests for the Contacts page and job-card badges, over a tmp SQLite DB."""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db
from app import app, get_db

CFG = {
    "default_ask": "Who handles engineering hiring?",
    "follow_up": {"first_business_days": 5, "second_business_days": 12},
    "weekly_targets": {
        "agency_first_touches": 5,
        "company_recruiter_emails_min": 5,
        "company_recruiter_emails_max": 8,
        "referral_touches": 1,
    },
    "company_suffixes": ["Inc", "Group"],
    "company_aliases": [["Acme", "Acme Holdings"]],
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
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    connection = db.get_connection(path, check_same_thread=False)
    db.init_db(connection)

    def override():
        request_conn = db.get_connection(path, check_same_thread=False)
        try:
            yield request_conn
        finally:
            request_conn.close()

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr(app_module, "_outreach_cfg", lambda: CFG)
    monkeypatch.setattr(app_module, "_today", lambda: "2026-03-02")
    monkeypatch.setattr(app_module, "_years_cfg", lambda: {"junior_max": 1, "senior_min": 5})
    yield connection
    app.dependency_overrides.clear()
    connection.close()


@pytest.fixture
def client(conn):
    return TestClient(app)


def contact(conn, **overrides):
    fields = {"name": "Pat Doe", "company": "Acme", "track": "company_recruiter",
              "source": "peer_list", "priority": "A", **overrides}
    return db.insert_contact(conn, **fields)


def blocked(conn):
    return contact(conn, name="Sam Blocked", company="Umbrella", priority="skip",
                   status="Skip", notes="Do not contact: former employer")


def job(conn, company="Acme", tracked=False):
    company_id = conn.execute(
        "INSERT INTO companies (name, ats_type, slug) VALUES (?, 'greenhouse', ?)",
        (company, company.lower().replace(" ", "-")),
    ).lastrowid
    job_id = conn.execute(
        """
        INSERT INTO jobs (company_id, external_id, title, score, score_breakdown, gate_failed)
        VALUES (?, 'x', 'Engineer', 50, '{}', 0)
        """,
        (company_id,),
    ).lastrowid
    conn.commit()
    return db.create_application(conn, job_id) if tracked else job_id


# The page


def test_page_groups_by_track_with_referrals_first(client, conn):
    contact(conn, name="Agency Person", track="agency")
    contact(conn, name="Real Friend", track="referral", source="own_pipeline", priority=None)
    html = client.get("/contacts").text
    assert html.index("Referrals (1)") < html.index("Agencies (1)")


def test_default_view_shows_unranked_and_hides_c(client, conn):
    contact(conn, name="Top Person", priority="A")
    contact(conn, name="Real Friend", track="referral", source="own_pipeline", priority=None)
    contact(conn, name="Backup Person", priority="C")
    html = client.get("/contacts").text
    assert "Top Person" in html
    assert "Real Friend" in html
    assert "Backup Person" not in html
    assert "Backup Person" in client.get("/contacts?show_c=1").text


def test_search_finds_c_contacts(client, conn):
    contact(conn, name="Backup Person", priority="C")
    assert "Backup Person" in client.get("/contacts/body?q=backup").text


def test_filters_narrow_the_body(client, conn):
    contact(conn, name="Agency Person", track="agency")
    contact(conn, name="Company Person")
    html = client.get("/contacts/body?track=agency").text
    assert "Agency Person" in html
    assert "Company Person" not in html


def test_empty_page_renders(client, conn):
    html = client.get("/contacts").text
    assert "No contacts match." in html
    assert "Nothing due." in html


def test_due_today_and_nav_count(client, conn):
    contact(conn, name="Overdue Person", status="Sent", date_contacted="2026-02-02")
    contact(conn, name="Fresh Person", status="Sent", date_contacted="2026-03-01")
    html = client.get("/contacts").text
    assert "First follow-up due" in html
    assert client.get("/contacts/due-count").text == "1"


def test_nav_count_is_empty_when_nothing_is_due(client, conn):
    assert client.get("/contacts/due-count").text == ""


def test_week_panel_shows_rate_and_targets(client, conn):
    contact(conn, status="Replied", next_step="Call", date_contacted="2026-03-02")
    contact(conn, name="Sam Roe", status="Sent", date_contacted="2026-03-02")
    html = client.get("/contacts").text
    assert "50%" in html
    assert "target 5 to 8" in html


def test_mentors_get_their_own_section_after_referrals(client, conn):
    contact(conn, name="Wise Mentor", track="mentor", source="own_pipeline", priority=None)
    contact(conn, name="Real Friend", track="referral", source="own_pipeline", priority=None)
    html = client.get("/contacts").text
    assert html.index("Referrals (1)") < html.index("Mentors / informational (1)")


def test_removed_contact_is_gone_from_the_page_and_modal(client, conn):
    contact_id = contact(conn, name="Gone Person")
    db.remove_contact(conn, contact_id)
    assert "Gone Person" not in client.get("/contacts?show_c=1").text
    assert client.get(f"/contacts/{contact_id}/modal").status_code == 404


# Skipped


def test_skipped_tab_shows_reasons_and_restore(client, conn):
    contact(conn, name="Skipped Person", status="Skip", priority="skip",
            notes="Not a recruiter")
    html = client.get("/contacts?tab=skipped").text
    assert "Skipped Person" in html
    assert "Not a recruiter" in html
    assert "/restore" in html


def test_skip_stays_out_of_the_active_view(client, conn):
    contact(conn, name="Skipped Person", status="Skip", priority="skip")
    assert "Skipped Person" not in client.get("/contacts?show_c=1").text


def test_do_not_contact_row_has_no_restore_button(client, conn):
    blocked(conn)
    html = client.get("/contacts?tab=skipped").text
    assert "Do not contact" in html
    assert "/restore" not in html


def test_restore_brings_a_contact_back_and_keeps_the_reason(client, conn):
    contact_id = contact(conn, status="Skip", priority="skip", notes="Not a recruiter")
    response = client.post(f"/contacts/{contact_id}/restore")
    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "contactsChanged"
    row = db.get_contact(conn, contact_id)
    assert (row["status"], row["priority"]) == ("Not started", "C")
    assert row["notes"] == "Not a recruiter"


def test_tab_counts_are_in_the_part_that_refreshes(client, conn):
    contact(conn, status="Skip", priority="skip")
    contact(conn, name="Sam Roe")
    html = client.get("/contacts/body").text
    assert "Active (1)" in html
    assert "Skipped (1)" in html


def test_restore_refuses_do_not_contact(client, conn):
    contact_id = blocked(conn)
    response = client.post(f"/contacts/{contact_id}/restore")
    assert response.status_code == 409
    assert "former employer" in response.json()["detail"]
    assert db.get_contact(conn, contact_id)["status"] == "Skip"


# Modal


def test_modal_shows_details_history_and_template(client, conn):
    contact_id = contact(conn, peer_history="Replied to them once")
    db.add_interaction(conn, contact_id, "2026-03-01", "email", "out", "First hello")
    html = client.get(f"/contacts/{contact_id}/modal").text
    assert "Replied to them once" in html
    assert "It isn't your relationship with them." in html
    assert "First hello" in html
    assert "Hi Pat, about Acme." in html


def test_modal_escapes_text_from_the_list(client, conn):
    contact_id = contact(conn, peer_history="<script>alert(1)</script>")
    html = client.get(f"/contacts/{contact_id}/modal").text
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_modal_unknown_contact(client, conn):
    assert client.get("/contacts/999/modal").status_code == 404


def test_do_not_contact_modal_has_no_template_or_status_control(client, conn):
    contact_id = blocked(conn)
    html = client.get(f"/contacts/{contact_id}/modal").text
    assert "Do not contact: former employer" in html
    assert "Suggested message" not in html
    assert 'name="status"' not in html


# Editing


def test_marking_sent_starts_the_clock_and_refreshes_the_page(client, conn):
    contact_id = contact(conn, status="Drafted")
    response = client.post(f"/contacts/{contact_id}", data={"status": "Sent"})
    assert response.headers["HX-Trigger"] == "contactsChanged"
    row = db.get_contact(conn, contact_id)
    assert row["status"] == "Sent"
    assert row["date_contacted"] == "2026-03-02"
    assert "follow-up due 2026-03-09" in response.text


def test_saving_an_unchanged_form_changes_nothing(client, conn):
    contact_id = contact(conn, status="Replied", next_step="Call", last_reply="2026-03-01")
    before = dict(db.get_contact(conn, contact_id))
    client.post(f"/contacts/{contact_id}", data={
        "status": "Replied", "priority": "A", "name": "Pat Doe", "company": "Acme",
        "email": "", "linkedin_url": "", "kind_detail": "", "ask": "",
        "next_step": "Call", "date_contacted": "", "last_reply": "2026-03-01",
        "verify_flag": "", "notes": "",
    })
    after = dict(db.get_contact(conn, contact_id))
    before.pop("updated_at"), after.pop("updated_at")
    assert after == before


def test_notes_append(client, conn):
    contact_id = contact(conn, notes="First")
    client.post(f"/contacts/{contact_id}", data={"notes": "Second"})
    assert db.get_contact(conn, contact_id)["notes"] == "First\n\nSecond"


def test_double_up_warning_shows_inline(client, conn):
    contact_id = contact(conn, company="Initrode", priority="B")
    response = client.post(f"/contacts/{contact_id}", data={"status": "Sent"})
    assert "already covered by Pat Doe" in response.text
    assert db.get_contact(conn, contact_id)["status"] == "Sent"


def test_refusal_shows_the_reason_and_saves_nothing(client, conn):
    contact_id = blocked(conn)
    response = client.post(f"/contacts/{contact_id}", data={"status": "Sent"})
    assert response.status_code == 200
    assert "Do not contact: former employer" in response.text
    assert "HX-Trigger" not in response.headers
    assert db.get_contact(conn, contact_id)["status"] == "Skip"


def test_email_in_use_is_refused(client, conn):
    contact(conn, email="pat@acme.example")
    other = contact(conn, name="Sam Roe")
    response = client.post(f"/contacts/{other}", data={"email": "PAT@acme.example"})
    assert "Email already used" in response.text
    assert db.get_contact(conn, other)["email"] is None


def test_bad_date_shows_an_error_not_a_crash(client, conn):
    contact_id = contact(conn)
    response = client.post(f"/contacts/{contact_id}", data={"last_reply": "soon"})
    assert response.status_code == 200
    assert 'class="status error"' in response.text


def test_edit_unknown_contact(client, conn):
    assert client.post("/contacts/999", data={"notes": "x"}).status_code == 404


# Interactions


def test_logging_a_reply_flags_it_for_a_response(client, conn):
    contact_id = contact(conn, status="Sent", date_contacted="2026-02-20",
                         next_step="Send the draft")
    response = client.post(f"/contacts/{contact_id}/interactions", data={
        "channel": "email", "direction": "in", "summary": "Happy to chat",
        "on_date": "2026-03-01",
    })
    assert response.headers["HX-Trigger"] == "contactsChanged"
    assert "respond within 24 hours" in response.text
    row = db.get_contact(conn, contact_id)
    assert (row["status"], row["last_reply"]) == ("Replied", "2026-03-01")


def test_blank_interaction_date_means_today(client, conn):
    contact_id = contact(conn)
    client.post(f"/contacts/{contact_id}/interactions", data={
        "channel": "linkedin", "direction": "out", "summary": "Connection note",
    })
    assert db.fetch_interactions(conn, contact_id)[0]["date"] == "2026-03-02"
    assert db.get_contact(conn, contact_id)["status"] == "Sent"


def test_outbound_to_do_not_contact_is_refused_and_not_logged(client, conn):
    contact_id = blocked(conn)
    response = client.post(f"/contacts/{contact_id}/interactions", data={
        "channel": "email", "direction": "out", "summary": "Hello",
    })
    assert "Do not contact: former employer" in response.text
    assert db.fetch_interactions(conn, contact_id) == []


def test_inbound_from_do_not_contact_still_logs(client, conn):
    contact_id = blocked(conn)
    client.post(f"/contacts/{contact_id}/interactions", data={
        "channel": "email", "direction": "in", "summary": "They wrote first",
    })
    assert len(db.fetch_interactions(conn, contact_id)) == 1
    assert db.get_contact(conn, contact_id)["status"] == "Skip"


@pytest.mark.parametrize(
    "data",
    [
        {"channel": "fax", "direction": "out", "summary": "Hi"},
        {"channel": "email", "direction": "out", "summary": "  "},
        {"channel": "email", "direction": "out", "summary": "Hi", "on_date": "soon"},
    ],
)
def test_bad_interaction_shows_an_error_and_logs_nothing(client, conn, data):
    contact_id = contact(conn)
    response = client.post(f"/contacts/{contact_id}/interactions", data=data)
    assert response.status_code == 200
    assert 'class="status error"' in response.text
    assert db.fetch_interactions(conn, contact_id) == []


# Adding


def test_add_contact(client, conn):
    response = client.post("/contacts", data={
        "track": "agency", "source": "own_pipeline", "name": "New Person",
        "company": "Globex", "priority": "", "email": "",
    })
    assert "Added New Person." in response.text
    assert response.headers["HX-Trigger"] == "contactsChanged"
    [row] = db.fetch_contacts(conn)
    assert row["source"] == "own_pipeline"
    assert row["priority"] is None
    assert row["email"] is None


def test_add_firm_with_no_person(client, conn):
    response = client.post("/contacts", data={"track": "agency", "company": "Globex"})
    assert "Added Globex." in response.text
    assert db.fetch_contacts(conn)[0]["name"] is None


def test_add_needs_a_name_or_company(client, conn):
    response = client.post("/contacts", data={"track": "agency"})
    assert "needs a name or a company" in response.text
    assert db.fetch_contacts(conn) == []


def test_add_duplicate_email_adds_nothing(client, conn):
    contact(conn, email="pat@acme.example")
    response = client.post("/contacts", data={"track": "agency", "name": "P",
                                              "email": "PAT@acme.example"})
    assert "Already saved: Pat Doe." in response.text
    assert len(db.fetch_contacts(conn)) == 1


def test_add_do_not_contact_saves_as_skip_with_a_warning(client, conn):
    response = client.post("/contacts", data={"track": "agency", "name": "Sam",
                                              "company": "Umbrella"})
    assert "Saved as Skip" in response.text
    assert db.fetch_contacts(conn)[0]["status"] == "Skip"


def test_add_warns_on_double_up(client, conn):
    response = client.post("/contacts", data={"track": "agency", "name": "Sam",
                                              "company": "Initrode"})
    assert "already covered by Pat Doe" in response.text


# Standing referrals


def watched(conn):
    job_id = job(conn)
    conn.execute(
        "UPDATE jobs SET title = 'Software Engineer', url = 'https://example.com/j', "
        "location = 'New York', description = '3+ years of experience' WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    return job_id, contact(conn, name="Sam Standing", track="referral",
                           source="own_pipeline", priority=None, standing_referral=1)


def test_matching_role_shows_in_due_today_and_the_nav_count(client, conn):
    watched(conn)
    html = client.get("/contacts").text
    assert "Send this role:" in html
    assert 'href="https://example.com/j"' in html
    assert "mid level: description asks for 3+ years of experience" in html
    assert client.get("/contacts/due-count").text == "1"


def test_nav_count_adds_watch_matches_to_follow_ups(client, conn):
    watched(conn)
    contact(conn, name="Overdue Person", status="Sent", date_contacted="2026-02-02")
    assert client.get("/contacts/due-count").text == "2"


def test_dismissing_a_match_clears_it_and_refreshes(client, conn):
    job_id, _ = watched(conn)
    response = client.post(f"/jobs/{job_id}/dismiss")
    assert response.headers["HX-Trigger"] == "contactsChanged"
    assert "Send this role:" not in client.get("/contacts").text
    assert client.get("/contacts/due-count").text == ""


def test_standing_referral_shows_on_the_row(client, conn):
    watched(conn)
    assert "Standing referral" in client.get("/contacts").text


def test_modal_checkbox_turns_the_watch_on_and_off(client, conn):
    contact_id = contact(conn, track="referral", source="own_pipeline")
    client.post(f"/contacts/{contact_id}", data={"standing_referral": ["0", "1"]})
    assert db.get_contact(conn, contact_id)["standing_referral"] == 1
    assert "checked" in client.get(f"/contacts/{contact_id}/modal").text
    client.post(f"/contacts/{contact_id}", data={"standing_referral": ["0"]})
    assert db.get_contact(conn, contact_id)["standing_referral"] == 0


# Badges


def test_feed_shows_referral_path_for_a_real_connection(client, conn):
    job(conn)
    contact(conn, source="own_pipeline", priority=None, track="referral")
    assert 'class="contact-badge path"' in client.get("/").text


def test_feed_shows_contact_for_a_or_b(client, conn):
    job(conn)
    contact(conn, priority="B")
    html = client.get("/").text
    assert ">Contact</a>" in html
    assert 'href="/contacts?q=Acme"' in html


def test_feed_matches_company_aliases(client, conn):
    job(conn)
    contact(conn, company="Acme Holdings")
    assert ">Contact</a>" in client.get("/").text


def test_feed_shows_no_badge_for_c_or_skip(client, conn):
    job(conn)
    contact(conn, priority="C")
    contact(conn, name="Sam Roe", priority="A", status="Skip")
    assert "contact-badge" not in client.get("/").text


def test_feed_shows_no_badge_for_a_mentor(client, conn):
    job(conn)
    contact(conn, track="mentor", source="own_pipeline", priority=None)
    assert "contact-badge" not in client.get("/").text


def test_feed_works_with_no_contacts(client, conn):
    job(conn)
    assert "contact-badge" not in client.get("/").text


def test_feed_partial_keeps_badges(client, conn):
    job(conn)
    contact(conn)
    assert ">Contact</a>" in client.get("/feed").text


def test_tracker_card_shows_a_badge(client, conn):
    job(conn, tracked=True)
    contact(conn, priority="A")
    assert ">Contact</a>" in client.get("/tracker").text


def test_tracker_card_shows_a_link_to_a_recruiter_at_another_company(client, conn):
    app_id = job(conn, tracked=True)
    contact_id = contact(conn, company="Staffing Firm", track="agency",
                         source="own_pipeline")
    assert "contact-badge" not in client.get("/tracker").text
    db.link_contact_application(conn, contact_id, app_id)
    assert 'class="contact-badge path"' in client.get("/tracker").text
