"""Tests for the Lever adapter's pure normalize() against a handcrafted fixture."""

import json
from pathlib import Path

import pytest

from scrapers import common, lever

FIXTURE = Path(__file__).parent / "fixtures" / "lever_board.json"


def load_fixture():
    return json.loads(FIXTURE.read_text())


def test_normalize_full_posting():
    jobs = lever.normalize(load_fixture())
    assert jobs[0] == {
        "external_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "title": "Senior Backend Engineer",
        "location": "Remote - US",
        "department": "Platform",
        "url": "https://jobs.lever.co/acme/a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        # Full JD: intro + every lists[] section (heading + de-HTMLed
        # bullets) + closing, not just Lever's intro-only descriptionPlain.
        "description": (
            "Build and scale our core APIs. Python, Postgres, AWS.\n\n"
            "Requirements\n• 5+ years with Python\n• Postgres & AWS\n\n"
            "Nice to have\n• Kubernetes\n\n"
            "We offer equity and full benefits."
        ),
        "posted_at": "2024-07-03 09:46:40",
    }


def test_full_description_from_lists_when_intro_empty():
    # Some postings put the entire JD in lists[] — an intro-only read
    # stored these as NULL descriptions.
    posting = {
        "descriptionPlain": "",
        "lists": [{"text": "About the role", "content": "<li>Ship things</li>"}],
    }
    assert lever._full_description(posting) == "About the role\n• Ship things"


def test_normalize_sparse_posting():
    jobs = lever.normalize(load_fixture())
    assert jobs[1] == {
        "external_id": "b2c3d4e5-f6a7-8901-bcde-f23456789012",
        "title": "Mystery Role",
        "location": None,
        "department": None,
        "url": None,
        "description": None,
        "posted_at": None,
    }


def test_normalize_skips_posting_without_text():
    raw = load_fixture()
    assert len(raw) == 3
    jobs = lever.normalize(raw)
    assert len(jobs) == 2
    skipped_id = "c3d4e5f6-a7b8-9012-cdef-345678901234"
    assert skipped_id not in [job["external_id"] for job in jobs]


def test_normalize_rejects_non_list_payload():
    with pytest.raises(common.FetchError):
        lever.normalize({"not": "a list"})
