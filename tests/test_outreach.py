from datetime import date

import pytest

import outreach

CFG = {
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
    assert fields == {"last_reply": "2026-03-02", "status": "Replied"}


@pytest.mark.parametrize(
    "status", ["Replied", "Call scheduled", "Referred / submitted", "Skip"]
)
def test_inbound_keeps_later_statuses_and_skip(status):
    fields = outreach.apply_interaction(
        contact(status=status), "in", "2026-03-02", CFG
    )
    assert fields == {"last_reply": "2026-03-02"}


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
