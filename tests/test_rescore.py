"""Tests for the rescore command: gates + scorer applied over the whole DB."""

import json
from datetime import datetime, timezone

import pytest

import db
from scoring.rescore import rescore_all
from scrapers.sweep import load_profile

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    connection.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'lever', 'acme')"
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def profile():
    return {
        "gates": {
            "excluded_title_keywords": ["recruiter", "staff"],
            "work_type": {"mine": "hybrid", "reject_conflicts": True},
            "allowed_locations": ["New York", "Remote"],
            "clearance_keywords": ["TS/SCI"],
            "max_posting_age_days": 30,
        },
        "scoring": {
            "target_titles": ["Software Engineer"],
            "title_match_weight": 30,
            "exact_title_bonus": 15,
            "required_keywords": ["react"],
            "required_keyword_weight": 5,
            "preferred_keywords": ["aws"],
            "preferred_keyword_weight": 2,
            "negative_keywords": ["php"],
            "negative_keyword_weight": -10,
            "must_haves": [],
            "missing_must_have_penalty": -20,
            "seniority": {
                "target": "mid",
                "multipliers": {"intern": 0.2, "junior": 0.75, "mid": 1.0,
                                "senior": 0.7, "staff": 0.4, "principal": 0.3},
            },
            "sparse_description": {"min_chars": 400, "multiplier": 0.85},
        },
    }


def insert_job(conn, external_id, **overrides):
    base = {
        "title": "Software Engineer",
        "location": "New York, NY",
        "description": "We build react apps on aws. " * 20,
        "posted_at": "2026-07-10 00:00:00",
    }
    row = {**base, **overrides}
    conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, location, description, posted_at)
           VALUES (1, ?, ?, ?, ?, ?)""",
        (external_id, row["title"], row["location"], row["description"], row["posted_at"]),
    )
    conn.commit()


def test_scores_passing_job_and_stores_breakdown(conn, profile):
    insert_job(conn, "j-pass")
    summary = rescore_all(conn, profile, now=NOW)
    assert summary == {
        "total": 1, "passed": 1, "rejected": 0, "rejections_by_gate": {},
    }
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["gate_failed"] == 0
    assert row["gate_reason"] is None
    assert row["score"] == 30 + 15 + 5 + 2  # title + exact + react + aws, mid x1.0
    breakdown = json.loads(row["score_breakdown"])
    assert breakdown["score"] == row["score"]
    assert breakdown["seniority"] == "mid"
    assert any("react" in c["reason"] for c in breakdown["components"])


def test_rejected_job_stores_reason_and_null_score(conn, profile):
    insert_job(conn, "j-fail", title="Staff Engineer", location="London")
    summary = rescore_all(conn, profile, now=NOW)
    assert summary["rejected"] == 1
    assert summary["rejections_by_gate"] == {"excluded-title": 1, "location": 1}
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["gate_failed"] == 1
    assert row["gate_reason"] == (
        "excluded-title: title contains 'staff'; "
        "location: 'London' matches none of allowed_locations"
    )
    assert row["score"] is None
    assert row["score_breakdown"] is None


def test_rescore_applies_profile_edits_and_flips_verdicts(conn, profile):
    insert_job(conn, "j-1")
    rescore_all(conn, profile, now=NOW)
    assert conn.execute("SELECT gate_failed FROM jobs").fetchone()[0] == 0

    profile["gates"]["excluded_title_keywords"].append("software")
    profile["scoring"]["required_keyword_weight"] = 50
    rescore_all(conn, profile, now=NOW)
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["gate_failed"] == 1
    assert "software" in row["gate_reason"]
    assert row["score"] is None

    # revert: the job comes back with the new weight applied
    profile["gates"]["excluded_title_keywords"].remove("software")
    rescore_all(conn, profile, now=NOW)
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["gate_failed"] == 0
    assert row["score"] == 30 + 15 + 50 + 2


def test_real_profile_yaml_drives_gates_and_scoring(conn):
    """Integration guard: the checked-in profile.yaml has every key we read."""
    real_profile = load_profile()
    insert_job(conn, "j-real", description="We build react and typescript apps. " * 20)
    insert_job(conn, "j-real-2", title="Recruiter", location="London")
    summary = rescore_all(conn, real_profile, now=NOW)
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["rejected"] == 1
