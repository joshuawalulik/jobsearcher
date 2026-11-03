"""Tests for stage-1 hard gates: every rejection carries a readable reason."""

from datetime import datetime, timezone

import pytest

from scoring.gates import check_gates, detect_work_type

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def gates_cfg():
    return {
        "excluded_title_keywords": ["staff", "recruiter", "ios", "qa", "site reliability"],
        "work_type": {"mine": "hybrid", "reject_conflicts": True},
        "allowed_locations": ["New York", "NYC", "Remote", "Distributed"],
        "clearance_keywords": ["TS/SCI", "top secret", "polygraph"],
        "max_posting_age_days": 30,
    }


def job(**overrides) -> dict:
    base = {
        "title": "Software Engineer",
        "location": "New York, NY",
        "description": "Build web apps with react and typescript.",
        "posted_at": "2026-07-10 12:00:00",
    }
    return {**base, **overrides}


def test_clean_job_passes(gates_cfg):
    result = check_gates(job(), gates_cfg, now=NOW)
    assert result.passed
    assert result.reasons == []


# --- excluded title keywords ------------------------------------------------

def test_excluded_title_keyword_rejects_with_reason(gates_cfg):
    result = check_gates(job(title="Staff Software Engineer"), gates_cfg, now=NOW)
    assert not result.passed
    assert result.reasons == ["excluded-title: title contains 'staff'"]


def test_excluded_title_is_word_boundary(gates_cfg):
    # "qa" must not hit "Qatar", "ios" must not hit "kiosks"
    result = check_gates(job(title="Engineer, Qatar kiosks team"), gates_cfg, now=NOW)
    assert result.passed


def test_excluded_multi_word_phrase(gates_cfg):
    result = check_gates(job(title="Site Reliability Engineer"), gates_cfg, now=NOW)
    assert "excluded-title: title contains 'site reliability'" in result.reasons


# --- location allowlist -----------------------------------------------------

@pytest.mark.parametrize(
    "location",
    [
        "New York, NY",
        "New York City",
        "NYC or Remote",
        "Remote - US",
        "United States - Remote",
        "Distributed",
        "San Francisco, CA • New York, NY • United States",
    ],
)
def test_real_world_locations_pass(gates_cfg, location):
    assert check_gates(job(location=location), gates_cfg, now=NOW).passed


@pytest.mark.parametrize(
    "location", ["San Francisco", "US-CA-Menlo Park", "London, UK", "Bengaluru, India"]
)
def test_disallowed_locations_reject(gates_cfg, location):
    result = check_gates(job(location=location), gates_cfg, now=NOW)
    assert not result.passed
    assert result.reasons == [f"location: '{location}' matches none of allowed_locations"]


def test_location_match_is_case_insensitive(gates_cfg):
    assert check_gates(job(location="remote (US)"), gates_cfg, now=NOW).passed


def test_missing_location_rejects_when_allowlist_set(gates_cfg):
    result = check_gates(job(location=None), gates_cfg, now=NOW)
    assert not result.passed
    assert "location:" in result.reasons[0]


def test_empty_allowlist_accepts_anywhere(gates_cfg):
    gates_cfg["allowed_locations"] = []
    assert check_gates(job(location="Bengaluru, India"), gates_cfg, now=NOW).passed


# --- work type --------------------------------------------------------------

def test_detect_work_type():
    assert detect_work_type("Remote - US") == "remote"
    assert detect_work_type("Distributed") == "remote"
    assert detect_work_type("Hybrid") == "hybrid"
    assert detect_work_type("NYC or Remote") == "remote"  # most permissive marker wins
    assert detect_work_type("New York, NY") is None
    assert detect_work_type(None) is None


def test_hybrid_profile_conflicts_with_nothing(gates_cfg):
    # A hybrid candidate can work remote, hybrid, or onsite postings;
    # geography is the location gate's job.
    for location in ("Remote - US", "New York, NY (Hybrid)", "New York, NY"):
        assert check_gates(job(location=location), gates_cfg, now=NOW).passed


def test_remote_profile_rejects_hybrid_posting(gates_cfg):
    gates_cfg["work_type"] = {"mine": "remote", "reject_conflicts": True}
    result = check_gates(job(location="New York, NY (Hybrid)"), gates_cfg, now=NOW)
    assert not result.passed
    assert result.reasons == [
        "work-type: posting is hybrid-marked, profile work type is remote"
    ]


def test_reject_conflicts_false_disables_gate(gates_cfg):
    gates_cfg["work_type"] = {"mine": "remote", "reject_conflicts": False}
    assert check_gates(job(location="New York, NY (Hybrid)"), gates_cfg, now=NOW).passed


# --- clearance --------------------------------------------------------------

def test_clearance_in_description_rejects(gates_cfg):
    result = check_gates(
        job(description="Must hold an active TS/SCI with polygraph."), gates_cfg, now=NOW
    )
    assert not result.passed
    assert result.reasons == ["clearance: posting mentions 'TS/SCI', 'polygraph'"]


# --- posting age ------------------------------------------------------------

def test_fresh_posting_passes(gates_cfg):
    assert check_gates(job(posted_at="2026-07-01 00:00:00"), gates_cfg, now=NOW).passed


def test_stale_posting_rejects_with_age(gates_cfg):
    result = check_gates(job(posted_at="2026-05-01 00:00:00"), gates_cfg, now=NOW)
    assert not result.passed
    assert result.reasons == ["age: posted 78 days ago (max 30)"]


def test_missing_or_unparseable_posted_at_passes(gates_cfg):
    assert check_gates(job(posted_at=None), gates_cfg, now=NOW).passed
    assert check_gates(job(posted_at="last Tuesday"), gates_cfg, now=NOW).passed


def test_age_gate_zero_or_absent_disables(gates_cfg):
    stale = job(posted_at="2025-01-01 00:00:00")
    gates_cfg["max_posting_age_days"] = 0
    assert check_gates(stale, gates_cfg, now=NOW).passed
    del gates_cfg["max_posting_age_days"]
    assert check_gates(stale, gates_cfg, now=NOW).passed


# --- multiple failures ------------------------------------------------------

def test_all_failing_gates_reported(gates_cfg):
    result = check_gates(
        job(title="Staff iOS Engineer", location="London, UK", posted_at="2026-01-01 00:00:00"),
        gates_cfg,
        now=NOW,
    )
    assert not result.passed
    prefixes = [reason.split(":", 1)[0] for reason in result.reasons]
    assert prefixes == ["excluded-title", "location", "age"]
    assert "excluded-title: title contains 'staff', 'ios'" in result.reasons


def test_experience_gate_rejects_over_cap(gates_cfg):
    gates_cfg["max_required_experience_years"] = 6
    result = check_gates(
        job(description="Requires 8+ years of experience with react."), gates_cfg, now=NOW
    )
    assert not result.passed
    assert result.reasons == ["experience: asks for 8 years (max 6)"]


def test_experience_gate_passes_at_cap(gates_cfg):
    gates_cfg["max_required_experience_years"] = 6
    result = check_gates(
        job(description="6+ years of experience building web apps."), gates_cfg, now=NOW
    )
    assert result.passed


def test_experience_gate_off_when_absent_or_zero(gates_cfg):
    demanding = job(description="Requires 15 years of experience.")
    assert check_gates(demanding, gates_cfg, now=NOW).passed  # key absent
    gates_cfg["max_required_experience_years"] = 0
    assert check_gates(demanding, gates_cfg, now=NOW).passed


def test_experience_gate_passes_without_years_demand(gates_cfg):
    gates_cfg["max_required_experience_years"] = 6
    assert check_gates(job(), gates_cfg, now=NOW).passed


def test_delisted_gate_rejects_stale_last_seen(gates_cfg):
    gates_cfg["delisted_after_days"] = 2
    result = check_gates(
        job(last_seen="2026-07-10 12:00:00"), gates_cfg, now=NOW  # 8 days stale
    )
    assert not result.passed
    assert result.reasons == ["delisted: last seen on the board 8 days ago (max 2)"]


def test_delisted_gate_passes_recent_last_seen(gates_cfg):
    gates_cfg["delisted_after_days"] = 2
    assert check_gates(job(last_seen="2026-07-17 12:00:00"), gates_cfg, now=NOW).passed


def test_delisted_gate_off_when_absent_zero_or_unverifiable(gates_cfg):
    stale = job(last_seen="2026-01-01 00:00:00")
    assert check_gates(stale, gates_cfg, now=NOW).passed  # key absent
    gates_cfg["delisted_after_days"] = 0
    assert check_gates(stale, gates_cfg, now=NOW).passed  # disabled
    gates_cfg["delisted_after_days"] = 2
    assert check_gates(job(), gates_cfg, now=NOW).passed  # no last_seen to check
    assert check_gates(job(last_seen="not-a-date"), gates_cfg, now=NOW).passed
