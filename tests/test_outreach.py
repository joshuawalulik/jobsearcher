from datetime import date

import pytest

import outreach

CFG = {
    "default_ask": "Who handles engineering hiring?",
    "templates": {
        "company_recruiter": {"subject": "Hello", "body": "Hi {first}, about {company}."},
        "agency": {"subject": "Searches", "body": "Hi {first}, any searches?"},
        "alumni": {"subject": "Fellow alum", "body": "Hi {first}, how is {company}?"},
        "follow_up": {"subject": "", "body": "Hi {first}. {new_detail} Thanks!"},
    },
    "follow_up": {"first_business_days": 5, "second_business_days": 12},
    "weekly_targets": {
        "agency_first_touches": 5,
        "company_recruiter_emails_min": 5,
        "company_recruiter_emails_max": 8,
        "referral_touches": 1,
    },
    "company_suffixes": ["Inc", "LLC", "Group", "Financial Group"],
    "company_aliases": [
        ["Acme Corp", "Acme", "ACME Holdings"],
        ["Globex", "Globex Consulting Group"],
    ],
    "dont_double_up": [
        {"company": "Initrode", "covered_by": "Pat Doe, Example Staffing"},
    ],
    "do_not_contact": [
        {
            "company": "Umbrella",
            "email_domains": ["umbrella.example"],
            "reason": "Former employer",
        },
    ],
}


def contact(**overrides):
    return {
        "id": 1,
        "name": "Pat Doe",
        "company": "Acme Corp",
        "email": None,
        "track": "company_recruiter",
        "status": "Not started",
        "date_contacted": None,
        "last_reply": None,
        "next_step": None,
        "source": "peer_list",
        "linkedin_url": None,
        "kind_detail": None,
        "priority": "C",
        "ask": None,
        "notes": None,
        "verify_flag": None,
        "gmail_draft_id": None,
        **overrides,
    }


# Company matching


@pytest.mark.parametrize(
    "a, b",
    [
        ("Acme Corp", "acme"),
        ("ACME Holdings", "Acme Corp."),
        ("Globex Consulting Group", "Globex"),
        ("Hooli, Inc.", "Hooli"),
        ("Hooli Group", "hooli llc"),
        ("Vandelay Financial Group", "Vandelay"),
        ("Hooli's", "Hoolis"),
        ("Hooli’s", "Hooli's"),
    ],
)
def test_company_names_match(a, b):
    assert outreach.same_company(a, b, CFG)


def test_different_companies_do_not_match():
    assert not outreach.same_company("Acme Corp", "Globex", CFG)


def test_blank_companies_never_match():
    assert outreach.company_key(None, CFG) == ""
    assert outreach.company_key("  ", CFG) == ""
    assert not outreach.same_company(None, None, CFG)


def test_suffix_only_strips_a_whole_trailing_word():
    assert outreach.company_key("Blogroup", CFG) == "blogroup"


# Business days


@pytest.mark.parametrize(
    "start, days, expected",
    [
        ("2026-03-02", 5, date(2026, 3, 9)),    # Mon to next Mon
        ("2026-03-06", 1, date(2026, 3, 9)),    # Fri skips the weekend
        ("2026-03-04", 12, date(2026, 3, 20)),  # crosses two weekends
        ("2026-03-07", 1, date(2026, 3, 9)),    # starts on a Saturday
        ("2026-03-02", 0, date(2026, 3, 2)),
    ],
)
def test_add_business_days(start, days, expected):
    assert outreach.add_business_days(start, days) == expected


# Follow-ups


@pytest.mark.parametrize(
    "status, expected",
    [
        ("Sent", date(2026, 3, 9)),
        ("Follow-up 1 sent", date(2026, 3, 18)),
        ("Follow-up 2 sent", None),
        ("Replied", None),
        ("Call scheduled", None),
        ("Referred / submitted", None),
        ("Cold", None),
        ("Skip", None),
        ("Drafted", None),
    ],
)
def test_follow_up_due_by_status(status, expected):
    row = contact(status=status, date_contacted="2026-03-02")
    assert outreach.follow_up_due(row, CFG) == expected


def test_no_follow_up_without_a_contact_date():
    assert outreach.follow_up_due(contact(status="Sent"), CFG) is None


def test_attention_on_the_due_date_but_not_before():
    row = contact(status="Sent", date_contacted="2026-03-02")
    assert outreach.attention_reason(row, "2026-03-06", CFG) is None
    assert outreach.attention_reason(row, "2026-03-09", CFG) == (
        "First follow-up due 2026-03-09"
    )
    assert "First follow-up" in outreach.attention_reason(row, "2026-03-20", CFG)


def test_attention_prompts_cold_after_second_follow_up():
    row = contact(status="Follow-up 2 sent", date_contacted="2026-03-02")
    assert "mark Cold" in outreach.attention_reason(row, "2026-03-03", CFG)


def test_attention_for_reply_without_next_step():
    assert "24 hours" in outreach.attention_reason(
        contact(status="Replied"), "2026-03-03", CFG
    )
    assert outreach.attention_reason(
        contact(status="Replied", next_step="Send resume"), "2026-03-03", CFG
    ) is None


def test_follow_ups_due_lists_oldest_first():
    rows = [
        contact(id=1, status="Sent", date_contacted="2026-03-02"),
        contact(id=2, status="Follow-up 1 sent", date_contacted="2026-02-02"),
        contact(id=3, status="Sent", date_contacted="2026-03-30"),
        contact(id=4, status="Replied"),
    ]
    due = outreach.follow_ups_due(rows, "2026-03-20", CFG)
    assert [c["id"] for c, _ in due] == [4, 2, 1]


# Do not contact and double-ups


def test_block_by_company_or_email_domain():
    assert outreach.block_reason("Umbrella", None, CFG) == (
        "Do not contact: Former employer"
    )
    assert outreach.block_reason("Acme Corp", "a@umbrella.example", CFG)
    assert outreach.block_reason("Acme Corp", "a@corp.umbrella.example", CFG)


def test_no_block_for_other_contacts():
    assert outreach.block_reason("Acme Corp", "a@acme.example", CFG) is None
    assert outreach.block_reason("Acme Corp", "a@notumbrella.example", CFG) is None
    assert outreach.block_reason(None, None, CFG) is None


def test_double_up_warning():
    assert outreach.double_up_warning("Initrode", CFG) == (
        "Initrode is already covered by Pat Doe, Example Staffing"
    )
    assert outreach.double_up_warning("Acme Corp", CFG) is None


# Status changes


def test_marking_sent_stamps_date_contacted():
    assert outreach.status_change(contact(), "Sent", "2026-03-02", CFG) == {
        "status": "Sent",
        "date_contacted": "2026-03-02",
    }


def test_status_change_keeps_existing_date_contacted():
    row = contact(status="Sent", date_contacted="2026-03-02")
    assert outreach.status_change(row, "Follow-up 1 sent", "2026-03-10", CFG) == {
        "status": "Follow-up 1 sent",
    }


def test_drafted_does_not_start_the_clock():
    assert outreach.status_change(contact(), "Drafted", "2026-03-02", CFG) == {
        "status": "Drafted",
    }


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        outreach.status_change(contact(), "Ghosted", "2026-03-02", CFG)


@pytest.mark.parametrize("status", ["Not started", "Drafted", "Sent"])
def test_blocked_contact_cannot_become_active(status):
    row = contact(company="Umbrella", status="Skip")
    with pytest.raises(ValueError, match="Do not contact"):
        outreach.status_change(row, status, "2026-03-02", CFG)


def test_blocked_contact_can_stay_skip():
    row = contact(company="Umbrella", status="Skip")
    assert outreach.status_change(row, "Skip", "2026-03-02", CFG) == {"status": "Skip"}


# Interactions


@pytest.mark.parametrize(
    "status",
    ["Not started", "Drafted", "Sent", "Follow-up 1 sent", "Follow-up 2 sent", "Cold"],
)
def test_inbound_moves_earlier_statuses_to_replied(status):
    fields = outreach.apply_interaction(
        contact(status=status), "in", "2026-03-02", CFG
    )
    assert fields == {"last_reply": "2026-03-02", "status": "Replied", "next_step": None}


@pytest.mark.parametrize(
    "status", ["Replied", "Call scheduled", "Referred / submitted", "Skip"]
)
def test_inbound_keeps_later_statuses_and_skip(status):
    fields = outreach.apply_interaction(
        contact(status=status), "in", "2026-03-02", CFG
    )
    assert fields == {"last_reply": "2026-03-02"}


def test_reply_with_a_stale_next_step_gets_flagged():
    row = contact(status="Sent", date_contacted="2026-03-02", next_step="Send the draft")
    fields = outreach.apply_interaction(row, "in", "2026-03-03", CFG)
    assert outreach.attention_reason({**row, **fields}, "2026-03-03", CFG) == (
        "Replied with no next step: respond within 24 hours"
    )


def test_marking_replied_keeps_a_new_next_step():
    row = contact(status="Sent", date_contacted="2026-03-02", next_step="Old plan")
    fields, _ = outreach.plan_update(
        row, {"status": "Replied", "next_step": "Send resume"}, "2026-03-03", CFG
    )
    assert fields == {"status": "Replied", "next_step": "Send resume"}


def test_marking_replied_without_a_next_step_clears_the_old_one():
    row = contact(status="Sent", date_contacted="2026-03-02", next_step="Old plan")
    fields, _ = outreach.plan_update(row, {"status": "Replied"}, "2026-03-03", CFG)
    assert fields == {"status": "Replied", "next_step": None}


def test_a_second_reply_keeps_the_current_next_step():
    row = contact(status="Replied", last_reply="2026-03-02", next_step="Send resume")
    assert outreach.apply_interaction(row, "in", "2026-03-03", CFG) == {
        "last_reply": "2026-03-03",
    }


def test_backdated_inbound_keeps_the_newer_last_reply():
    row = contact(status="Replied", last_reply="2026-03-09")
    assert outreach.apply_interaction(row, "in", "2026-03-02", CFG) == {}


@pytest.mark.parametrize(
    "status, expected",
    [
        ("Not started", "Sent"),
        ("Drafted", "Sent"),
        ("Sent", "Follow-up 1 sent"),
        ("Follow-up 1 sent", "Follow-up 2 sent"),
    ],
)
def test_outbound_advances_the_send_sequence(status, expected):
    fields = outreach.apply_interaction(
        contact(status=status, date_contacted="2026-03-02"),
        "out", "2026-03-10", CFG,
    )
    assert fields == {"status": expected}


def test_first_outbound_starts_the_clock():
    fields = outreach.apply_interaction(contact(), "out", "2026-03-02", CFG)
    assert fields == {"status": "Sent", "date_contacted": "2026-03-02"}


@pytest.mark.parametrize(
    "status", ["Follow-up 2 sent", "Replied", "Call scheduled", "Cold", "Skip"]
)
def test_outbound_leaves_other_statuses_alone(status):
    assert outreach.apply_interaction(
        contact(status=status), "out", "2026-03-02", CFG
    ) == {}


def test_blocked_contact_cannot_log_outbound_but_can_log_inbound():
    row = contact(company="Acme Corp", email="a@umbrella.example", status="Skip")
    with pytest.raises(ValueError, match="Do not contact"):
        outreach.apply_interaction(row, "out", "2026-03-02", CFG)
    assert outreach.apply_interaction(row, "in", "2026-03-02", CFG) == {
        "last_reply": "2026-03-02",
    }


def test_unknown_direction_is_rejected():
    with pytest.raises(ValueError):
        outreach.apply_interaction(contact(), "sideways", "2026-03-02", CFG)


# Referral paths


def test_own_pipeline_contact_is_a_path():
    assert outreach.has_referral_path(contact(source="own_pipeline"))


def test_reference_list_contact_is_not_a_path_until_they_reply():
    assert not outreach.has_referral_path(contact(source="peer_list", status="Sent"))
    assert outreach.has_referral_path(
        contact(source="peer_list", status="Replied", last_reply="2026-03-02")
    )


@pytest.mark.parametrize("status", ["Cold", "Skip"])
def test_inactive_contacts_are_never_a_path(status):
    assert not outreach.has_referral_path(
        contact(source="own_pipeline", status=status)
    )


# Reply rate and weekly summary


def _rows(*statuses):
    return [contact(id=i, status=s) for i, s in enumerate(statuses, 1)]


def test_reply_rate_matches_the_seed_workbook_summary():
    agencies = _rows("Replied", "Drafted", *["Not started"] * 10)
    company = _rows(*["Drafted"] * 9, *["Not started"] * 9, *["Skip"] * 4)
    referrals = _rows(
        "Sent", "Replied", "Referred / submitted", "Drafted", "Drafted",
        *["Not started"] * 3,
    )
    assert outreach.reply_rate(agencies) == 1.0
    assert outreach.reply_rate(company) == 0.0
    assert outreach.reply_rate(referrals) == pytest.approx(2 / 3)
    assert outreach.reply_rate(agencies + company + referrals) == 0.75


def test_cold_counts_against_reply_rate():
    assert outreach.reply_rate(_rows("Replied", "Cold")) == 0.5


def test_weekly_summary_counts_and_progress():
    rows = [
        contact(id=1, track="agency", status="Sent", date_contacted="2026-03-02"),
        contact(id=2, track="agency", status="Sent", date_contacted="2026-02-23"),
        contact(id=3, status="Sent", date_contacted="2026-03-08"),
        contact(id=4, track="referral", status="Replied", next_step="Call"),
        contact(id=5, status="Skip"),
    ]
    interactions = [
        {"contact_id": 4, "date": "2026-03-04", "direction": "out"},
        {"contact_id": 4, "date": "2026-03-05", "direction": "in"},
        {"contact_id": 4, "date": "2026-02-20", "direction": "out"},
        {"contact_id": 1, "date": "2026-03-04", "direction": "out"},
    ]
    summary = outreach.weekly_summary(rows, interactions, "2026-03-04", CFG)
    assert summary["week_start"] == "2026-03-02"
    assert summary["week_end"] == "2026-03-08"
    assert summary["by_track"]["agency"] == {"Sent": 2}
    assert summary["by_status"] == {"Sent": 3, "Replied": 1, "Skip": 1}
    assert summary["follow_ups_due"] == 1  # the Feb 23 agency contact
    assert summary["reply_rate"] == 0.25
    assert summary["progress"] == {
        "agency_first_touches": {"done": 1, "target": 5},
        "company_recruiter_emails": {"done": 1, "target_min": 5, "target_max": 8},
        "referral_touches": {"done": 1, "target": 1},
    }


def test_weekly_summary_with_no_contacts():
    summary = outreach.weekly_summary([], [], "2026-03-04", CFG)
    assert summary["by_status"] == {}
    assert summary["reply_rate"] == 0.0
    assert summary["follow_ups_due"] == 0


# Summaries and filters


def test_summary_leaves_out_peer_history_and_flags_action():
    row = contact(status="Sent", date_contacted="2026-03-02", peer_history="Long text",
                  gmail_draft_id="d1", source="own_pipeline")
    summary = outreach.contact_summary(row, "2026-03-09", CFG)
    assert "peer_history" not in summary
    assert summary["contact_id"] == 1
    assert summary["follow_up_due"] == "2026-03-09"
    assert summary["needs_action"] == "First follow-up due 2026-03-09"
    assert summary["referral_path"] is True
    assert summary["has_gmail_draft"] is True
    assert summary["do_not_contact"] is None


def test_summary_marks_blocked_contacts():
    row = contact(company="Umbrella", status="Skip")
    summary = outreach.contact_summary(row, "2026-03-09", CFG)
    assert summary["do_not_contact"] == "Do not contact: Former employer"


def test_filter_hides_skip_unless_asked():
    rows = [contact(id=1), contact(id=2, status="Skip", priority="skip")]
    assert [c["id"] for c in outreach.filter_contacts(rows, CFG)] == [1]
    skipped = outreach.filter_contacts(rows, CFG, status="Skip")
    assert [c["id"] for c in skipped] == [2]


def test_filter_sorts_by_priority_then_name():
    rows = [
        contact(id=1, name="Zed", priority="C"),
        contact(id=2, name="Bea", priority="A"),
        contact(id=3, name=None, priority=None),
        contact(id=4, name="Amy", priority="A"),
    ]
    assert [c["id"] for c in outreach.filter_contacts(rows, CFG)] == [4, 2, 1, 3]


def test_filter_by_track_priority_and_company():
    rows = [
        contact(id=1, company="Acme Corp", track="agency", priority="A"),
        contact(id=2, company="Globex", track="agency", priority="B"),
        contact(id=3, company="Vandelay Industries", priority="B"),
    ]
    ids = lambda **f: [c["id"] for c in outreach.filter_contacts(rows, CFG, **f)]
    assert ids(track="agency") == [1, 2]
    assert ids(priority="B") == [2, 3]
    assert ids(company="ACME Holdings") == [1]
    assert ids(company="vandelay") == [3]
    assert ids(track="agency", priority="A") == [1]


# Contacts for a job


def test_contacts_for_company_groups_and_flags_a_real_path():
    rows = [
        contact(id=1, company="Acme", track="company_recruiter"),
        contact(id=2, company="Acme Corp", track="referral", source="own_pipeline"),
        contact(id=3, company="Globex"),
    ]
    result = outreach.contacts_for_company(rows, "Acme Corp", set(), "2026-03-02", CFG)
    assert result["referral_path"] is True
    assert result["referral_path_contacts"] == ["Pat Doe"]
    assert set(result["contacts_by_track"]) == {"company_recruiter", "referral"}


def test_reference_list_contact_alone_is_not_a_path():
    rows = [contact(id=1, company="Acme", priority="A")]
    result = outreach.contacts_for_company(rows, "Acme", set(), "2026-03-02", CFG)
    assert result["referral_path"] is False
    assert result["referral_path_contacts"] == []
    assert len(result["contacts_by_track"]["company_recruiter"]) == 1


def test_contacts_for_company_leaves_out_skip_and_cold():
    rows = [
        contact(id=1, company="Acme", status="Skip"),
        contact(id=2, company="Acme", status="Cold", source="own_pipeline"),
    ]
    result = outreach.contacts_for_company(rows, "Acme", set(), "2026-03-02", CFG)
    assert result["contacts_by_track"] == {}
    assert result["referral_path"] is False


def test_linked_contact_at_another_company_is_included():
    rows = [contact(id=1, company="Example Staffing", track="agency",
                    source="own_pipeline")]
    result = outreach.contacts_for_company(rows, "Acme", {1}, "2026-03-02", CFG)
    assert result["referral_path"] is True
    assert len(result["contacts_by_track"]["agency"]) == 1


def test_contacts_for_company_passes_on_a_double_up_warning():
    result = outreach.contacts_for_company([], "Initrode", set(), "2026-03-02", CFG)
    assert "already covered" in result["double_up_warning"]


# Templates


def test_company_recruiter_template_is_filled_in():
    assert outreach.suggest_template(contact(name="Pat Q. Doe"), CFG) == {
        "kind": "company_recruiter",
        "subject": "Hello",
        "body": "Hi Pat, about Acme Corp.",
    }


def test_agency_template():
    row = contact(track="agency")
    assert outreach.suggest_template(row, CFG)["kind"] == "agency"


def test_alumni_first_touch_works_on_the_referral_track():
    row = contact(track="referral", kind_detail="Fellow alum")
    assert outreach.suggest_template(row, CFG)["kind"] == "alumni"


def test_alumni_label_picks_the_alumni_template():
    row = contact(kind_detail="State University alum")
    assert outreach.suggest_template(row, CFG)["kind"] == "alumni"


def test_contacted_person_gets_the_follow_up_with_detail_left_open():
    result = outreach.suggest_template(contact(status="Sent"), CFG)
    assert result["kind"] == "follow_up"
    assert result["body"] == "Hi Pat. {new_detail} Thanks!"


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": None},
        {"status": "Skip"},
        {"status": "Cold"},
        {"status": "Replied"},
        {"status": "Follow-up 2 sent"},
        {"track": "referral"},
        {"track": "platform"},
        {"company": "Umbrella"},
        {"track": "referral", "status": "Sent"},
        {"track": "referral", "status": "Follow-up 1 sent"},
        {"track": "platform", "status": "Sent"},
    ],
)
def test_no_template_when_a_message_would_be_wrong(overrides):
    assert outreach.suggest_template(contact(**overrides), CFG) is None


# Updates


def test_update_writes_plain_fields():
    fields, warnings = outreach.plan_update(
        contact(), {"next_step": " Call Monday ", "priority": "A"}, "2026-03-02", CFG
    )
    assert fields == {"next_step": "Call Monday", "priority": "A"}
    assert warnings == []


def test_update_rejects_fixed_and_unknown_fields():
    for field in ("track", "source", "peer_history", "nonsense"):
        with pytest.raises(ValueError, match="can't update"):
            outreach.plan_update(contact(), {field: "x"}, "2026-03-02", CFG)


def test_update_rejects_bad_values():
    with pytest.raises(ValueError):
        outreach.plan_update(contact(), {"priority": "Z"}, "2026-03-02", CFG)
    with pytest.raises(ValueError):
        outreach.plan_update(contact(), {"last_reply": "soon"}, "2026-03-02", CFG)
    with pytest.raises(ValueError):
        outreach.plan_update(contact(), {"status": "Ghosted"}, "2026-03-02", CFG)


def test_update_notes_append():
    row = contact(notes="Skip reason\n")
    fields, _ = outreach.plan_update(row, {"notes": "New note"}, "2026-03-02", CFG)
    assert fields == {"notes": "Skip reason\n\nNew note"}


def test_blank_notes_never_erase_existing_notes():
    fields, _ = outreach.plan_update(
        contact(notes="Keep"), {"notes": "  "}, "2026-03-02", CFG
    )
    assert fields == {}


def test_marking_sent_starts_the_clock():
    fields, _ = outreach.plan_update(
        contact(status="Drafted"), {"status": "Sent"}, "2026-03-02", CFG
    )
    assert fields == {"status": "Sent", "date_contacted": "2026-03-02"}


def test_saving_a_draft_id_moves_not_started_to_drafted():
    fields, _ = outreach.plan_update(
        contact(), {"gmail_draft_id": "d1"}, "2026-03-02", CFG
    )
    assert fields == {"gmail_draft_id": "d1", "status": "Drafted"}


def test_saving_a_draft_id_leaves_later_statuses_alone():
    fields, _ = outreach.plan_update(
        contact(status="Sent", date_contacted="2026-03-02"),
        {"gmail_draft_id": "d1"}, "2026-03-03", CFG,
    )
    assert fields == {"gmail_draft_id": "d1"}


@pytest.mark.parametrize("status", ["Drafted", "Sent"])
def test_double_up_warns_when_drafting_or_sending(status):
    row = contact(company="Initrode", status="Not started")
    fields, warnings = outreach.plan_update(row, {"status": status}, "2026-03-02", CFG)
    assert fields["status"] == status
    assert warnings == ["Initrode is already covered by Pat Doe, Example Staffing"]


def test_double_up_does_not_warn_on_unrelated_edits():
    row = contact(company="Initrode")
    _, warnings = outreach.plan_update(row, {"next_step": "x"}, "2026-03-02", CFG)
    assert warnings == []


def test_update_cannot_leave_skip():
    with pytest.raises(ValueError, match="restore_contact"):
        outreach.plan_update(
            contact(status="Skip"), {"status": "Not started"}, "2026-03-02", CFG
        )


def test_blocked_contact_refuses_active_changes():
    row = contact(company="Umbrella", status="Skip", priority="skip")
    for changes in ({"status": "Sent"}, {"gmail_draft_id": "d1"}, {"priority": "A"}):
        with pytest.raises(ValueError, match="Do not contact"):
            outreach.plan_update(row, changes, "2026-03-02", CFG)


def test_blocked_contact_can_still_take_notes():
    row = contact(company="Umbrella", status="Skip", priority="skip")
    fields, _ = outreach.plan_update(row, {"notes": "Saw them at an event"}, "2026-03-02", CFG)
    assert fields == {"notes": "Saw them at an event"}


def test_new_email_that_is_blocked_moves_the_contact_to_skip():
    fields, warnings = outreach.plan_update(
        contact(status="Drafted", gmail_draft_id="d1"),
        {"email": "pat@umbrella.example"}, "2026-03-02", CFG,
    )
    assert fields["status"] == "Skip"
    assert fields["priority"] == "skip"
    assert fields["gmail_draft_id"] is None
    assert "Moved to Skip" in warnings[0]


# New contacts


def test_new_contact_defaults():
    new, warnings = outreach.plan_new_contact(
        {"name": "Pat Doe", "company": "Acme", "track": "agency", "email": None},
        [], CFG,
    )
    assert new == {"name": "Pat Doe", "company": "Acme", "track": "agency",
                   "source": "research"}
    assert warnings == []


def test_firm_only_contact_is_allowed():
    new, _ = outreach.plan_new_contact({"company": "Globex", "track": "agency"}, [], CFG)
    assert new["company"] == "Globex"


@pytest.mark.parametrize(
    "fields",
    [
        {"track": "agency"},
        {"name": "Pat Doe", "track": "nonsense"},
        {"name": "Pat Doe"},
        {"name": "Pat Doe", "track": "agency", "source": "nonsense"},
    ],
)
def test_new_contact_rejects_bad_input(fields):
    with pytest.raises(ValueError):
        outreach.plan_new_contact(fields, [], CFG)


def test_blocked_new_contact_is_saved_as_skip():
    new, warnings = outreach.plan_new_contact(
        {"name": "Pat Doe", "email": "pat@umbrella.example", "track": "agency"}, [], CFG
    )
    assert new["status"] == "Skip"
    assert new["priority"] == "skip"
    assert new["notes"] == "Do not contact: Former employer"
    assert "Saved as Skip" in warnings[0]


def test_new_contact_warns_on_a_double_up():
    _, warnings = outreach.plan_new_contact(
        {"name": "Sam Roe", "company": "Initrode", "track": "company_recruiter"}, [], CFG
    )
    assert "already covered" in warnings[0]


def test_new_contact_warns_on_same_name_and_company():
    existing = [contact(id=7, name="Pat Doe", company="Acme Corp")]
    _, warnings = outreach.plan_new_contact(
        {"name": "pat doe", "company": "Acme", "track": "agency"}, existing, CFG
    )
    assert warnings == ["Possible duplicate of contact 7: same name and company"]


def test_find_duplicate_ignores_case():
    existing = [contact(id=7, email="Pat@Acme.example")]
    assert outreach.find_duplicate(" pat@acme.EXAMPLE ", existing)["id"] == 7
    assert outreach.find_duplicate("sam@acme.example", existing) is None
    assert outreach.find_duplicate(None, existing) is None


# Restore


def test_restore_sets_status_priority_and_ask():
    row = contact(status="Skip", priority="skip", notes="Not a recruiter")
    assert outreach.restore_fields(row, CFG) == {
        "status": "Not started",
        "priority": "C",
        "ask": CFG["default_ask"],
    }


def test_restore_keeps_a_real_priority_and_existing_ask():
    row = contact(status="Skip", priority="A", ask="Ask about teams")
    assert outreach.restore_fields(row, CFG) == {"status": "Not started"}


def test_restore_refuses_blocked_contacts():
    row = contact(email="pat@umbrella.example", status="Skip", priority="skip")
    with pytest.raises(ValueError, match="Do not contact"):
        outreach.restore_fields(row, CFG)


def test_restore_only_applies_to_skipped_contacts():
    with pytest.raises(ValueError, match="Only skipped"):
        outreach.restore_fields(contact(status="Sent"), CFG)


# Contacts page


def test_search_finds_names_emails_and_company_aliases():
    rows = [
        contact(id=1, name="Pat Doe", company="Acme Corp"),
        contact(id=2, name="Sam Roe", company="Globex", email="sam@roe.example"),
    ]
    ids = lambda text: [c["id"] for c in outreach.filter_contacts(rows, CFG, search=text)]
    assert ids("pat") == [1]
    assert ids("roe.example") == [2]
    assert ids("ACME Holdings") == [1]
    assert ids("Globex Consulting Group") == [2]
    assert ids("nobody") == []
    assert ids("...") == []


def test_default_view_hides_c_but_keeps_unranked_people():
    rows = [
        contact(id=1, priority="A"),
        contact(id=2, priority="B"),
        contact(id=3, priority="C"),
        contact(id=4, priority=None, track="referral"),
        contact(id=5, priority="skip", status="Skip"),
    ]
    assert {c["id"] for c in outreach.visible_contacts(rows, CFG)} == {1, 2, 4}
    assert {c["id"] for c in outreach.visible_contacts(rows, CFG, show_c=True)} == {1, 2, 3, 4}


@pytest.mark.parametrize(
    "filters",
    [{"search": "Pat"}, {"priority": "C"}, {"status": "Not started"}],
)
def test_looking_for_someone_finds_c_contacts(filters):
    rows = [contact(id=1, priority="C")]
    assert len(outreach.visible_contacts(rows, CFG, **filters)) == 1


def test_a_track_filter_alone_keeps_c_hidden():
    rows = [contact(id=1, priority="C", track="agency")]
    assert outreach.visible_contacts(rows, CFG, track="agency") == []


def test_groups_put_real_relationships_first_and_skip_empty_tracks():
    rows = [
        contact(id=1, track="platform"),
        contact(id=2, track="company_recruiter"),
        contact(id=3, track="referral"),
    ]
    assert [track for track, _ in outreach.group_by_track(rows)] == [
        "referral", "company_recruiter", "platform",
    ]


# Badges


def test_badge_is_a_referral_path_for_a_real_connection():
    rows = [contact(company="Acme", source="own_pipeline", priority=None)]
    assert outreach.company_badges(rows, CFG) == {"acme corp": "Referral path"}


def test_badge_is_contact_for_a_or_b():
    rows = [contact(company="Acme", priority="B")]
    assert outreach.company_badges(rows, CFG) == {"acme corp": "Contact"}


def test_no_badge_for_c_skip_cold_or_no_company():
    rows = [
        contact(id=1, company="Acme", priority="C"),
        contact(id=2, company="Globex", priority="A", status="Skip"),
        contact(id=3, company="Hooli", source="own_pipeline", status="Cold"),
        contact(id=4, company=None, source="own_pipeline"),
    ]
    assert outreach.company_badges(rows, CFG) == {}


@pytest.mark.parametrize("reverse", [False, True])
def test_a_real_connection_beats_a_contact_in_either_order(reverse):
    rows = [
        contact(id=1, company="Acme", priority="A"),
        contact(id=2, company="Acme", source="own_pipeline"),
    ]
    rows = rows[::-1] if reverse else rows
    assert outreach.company_badges(rows, CFG) == {"acme corp": "Referral path"}


def test_a_contact_who_replies_upgrades_the_badge():
    rows = [contact(company="Acme", priority="C", status="Replied",
                    last_reply="2026-03-02")]
    assert outreach.company_badges(rows, CFG) == {"acme corp": "Referral path"}


def test_linked_badges_need_a_real_connection():
    rows = [
        contact(id=1, company="Staffing", source="own_pipeline"),
        contact(id=2, company="Staffing", source="peer_list"),
    ]
    links = [
        {"contact_id": 1, "application_id": 10},
        {"contact_id": 2, "application_id": 20},
    ]
    assert outreach.linked_badges(rows, links) == {10: "Referral path"}


def test_check_interaction():
    outreach.check_interaction("email", "out", "Hello")
    for args in (("fax", "out", "Hi"), ("email", "up", "Hi"), ("email", "out", " ")):
        with pytest.raises(ValueError):
            outreach.check_interaction(*args)


# Mentors


def mentor(**overrides):
    return contact(track="mentor", source="own_pipeline", **overrides)


def test_mentor_has_no_follow_up_clock():
    row = mentor(status="Sent", date_contacted="2026-03-02")
    assert outreach.follow_up_due(row, CFG) is None
    assert outreach.attention_reason(row, "2026-06-01", CFG) is None


def test_a_mentor_reply_still_gets_flagged():
    row = mentor(status="Replied", last_reply="2026-03-02")
    assert "24 hours" in outreach.attention_reason(row, "2026-03-03", CFG)


def test_mentor_is_not_a_referral_path_or_a_badge():
    row = mentor(company="Acme", priority=None, status="Replied", last_reply="2026-03-02")
    assert outreach.has_referral_path(row) is False
    assert outreach.company_badges([row], CFG) == {}


def test_mentor_gets_no_template():
    assert outreach.suggest_template(mentor(), CFG) is None
    assert outreach.suggest_template(mentor(kind_detail="Fellow alum"), CFG) is None


def test_mentors_stay_out_of_reply_rate():
    rows = [
        contact(id=1, status="Replied"),
        contact(id=2, status="Sent"),
        mentor(id=3, status="Sent"),
        mentor(id=4, status="Sent"),
    ]
    assert outreach.reply_rate(rows) == 0.5


def test_mentor_touches_are_not_referral_touches():
    rows = [mentor(id=1)]
    interactions = [{"contact_id": 1, "date": "2026-03-04", "direction": "out"}]
    summary = outreach.weekly_summary(rows, interactions, "2026-03-04", CFG)
    assert summary["progress"]["referral_touches"]["done"] == 0


def test_mentors_group_after_referrals():
    rows = [contact(id=1, track="agency"), mentor(id=2), contact(id=3, track="referral")]
    assert [t for t, _ in outreach.group_by_track(rows)] == ["referral", "mentor", "agency"]


def test_mentor_can_be_added():
    new, _ = outreach.plan_new_contact({"name": "Pat Doe", "track": "mentor"}, [], CFG)
    assert new["track"] == "mentor"
