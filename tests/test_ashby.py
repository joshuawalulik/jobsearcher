"""Tests for the Ashby adapter's normalize() against a handcrafted fixture."""

import json
from pathlib import Path

import pytest

from scrapers import ashby, common

FIXTURE = Path(__file__).parent / "fixtures" / "ashby_board.json"


def load_fixture():
    return json.loads(FIXTURE.read_text())


def test_normalize_full_posting():
    jobs = ashby.normalize(load_fixture())
    assert jobs[0] == {
        "external_id": "5b2e2c1a-9f4d-4a6e-8b7c-0d1e2f3a4b5c",
        "title": "Senior Backend Engineer",
        "location": "Remote - US",
        "department": "Engineering",
        "url": "https://jobs.ashbyhq.com/acme/5b2e2c1a-9f4d-4a6e-8b7c-0d1e2f3a4b5c",
        "description": "Build and scale our backend services.",
        "posted_at": "2026-07-01 12:34:56",
    }


def test_normalize_html_fallback_and_missing_fields():
    jobs = ashby.normalize(load_fixture())
    assert jobs[1] == {
        "external_id": "0a1b2c3d-4e5f-6a7b-8c9d-e0f1a2b3c4d5",
        "title": "Data Engineer",
        "location": None,
        "department": None,
        "url": "https://jobs.ashbyhq.com/acme/0a1b2c3d-4e5f-6a7b-8c9d-e0f1a2b3c4d5",
        "description": "About the role Own our data & ETL pipelines.",
        "posted_at": "2026-06-20 07:15:30",
    }


def test_normalize_drops_unlisted_posting():
    raw = load_fixture()
    assert len(raw["jobs"]) == 3
    jobs = ashby.normalize(raw)
    assert len(jobs) == 2
    assert all(job["external_id"] != "9c8d7e6f-5a4b-3c2d-1e0f-a1b2c3d4e5f6" for job in jobs)


def test_normalize_rejects_error_shaped_payload():
    with pytest.raises(common.FetchError):
        ashby.normalize({"errors": ["board not found"]})
