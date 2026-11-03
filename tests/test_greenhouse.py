"""Tests for the Greenhouse adapter's normalize() against a fixture payload."""

import json
from pathlib import Path

import pytest

from scrapers import common, greenhouse

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "greenhouse_board.json"


@pytest.fixture
def raw_board():
    return json.loads(FIXTURE_PATH.read_text())


def test_normalize_fully_populated_posting(raw_board):
    posting = greenhouse.normalize(raw_board)[0]
    assert posting == {
        "external_id": "4000001",
        "title": "Senior Backend Engineer",
        "location": "New York, NY",
        "department": "Engineering",
        "url": "https://boards.greenhouse.io/acme/jobs/4000001",
        "description": "Build APIs in Python. FastAPI SQLite",
        "posted_at": "2026-07-01 16:34:56",
    }


def test_normalize_sparse_posting_falls_back_to_updated_at(raw_board):
    posting = greenhouse.normalize(raw_board)[1]
    assert posting == {
        "external_id": "4000002",
        "title": "Data Analyst",
        "location": None,
        "department": None,
        "url": "https://boards.greenhouse.io/acme/jobs/4000002",
        "description": None,
        "posted_at": "2026-07-03 04:15:00",
    }


def test_normalize_skips_posting_without_title(raw_board):
    postings = greenhouse.normalize(raw_board)
    assert len(postings) == 2
    assert "4000003" not in {p["external_id"] for p in postings}


def test_normalize_raises_on_missing_jobs_key():
    with pytest.raises(common.FetchError):
        greenhouse.normalize({"meta": {}})


def test_normalize_keeps_absolute_url_as_is(raw_board):
    # Links go straight to the posting's own URL — company-hosted or
    # greenhouse-hosted alike (embed rewriting was tried and reverted).
    raw_board["jobs"][0]["absolute_url"] = "https://www.mongodb.com/careers/job/?gh_jid=8054991"
    posting = greenhouse.normalize(raw_board, "mongodb")[0]
    assert posting["url"] == "https://www.mongodb.com/careers/job/?gh_jid=8054991"
    raw_board["jobs"][0]["absolute_url"] = "https://job-boards.greenhouse.io/acme/jobs/123"
    assert greenhouse.normalize(raw_board)[0]["url"] == "https://job-boards.greenhouse.io/acme/jobs/123"
