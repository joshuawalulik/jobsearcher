"""profile_io: comment-preserving read-modify-write of profile.yaml.

Runs against a tmp copy of the real config/profile.yaml so the tests
prove the actual file's comments survive, not just a toy fixture's.
"""

import shutil
from pathlib import Path

import pytest
import yaml

import profile_io

REAL_PROFILE = Path(__file__).parent.parent / "config" / "profile.yaml"

# Comments from the real file that must survive every edit.
SENTINEL_COMMENTS = [
    "# Verified 2026-07-16: every slug returned 200 with live jobs from its ATS API.",
    "# NOTE: short keywords (ai, api, rest, java, css) require word-boundary",
    "# US-remote only — bare \"Remote\" let through Remote-India/Canada/etc.",
    "# also 'Remote - USA', 'Remote - US: ...'",
]


@pytest.fixture
def profile_path(tmp_path):
    path = tmp_path / "profile.yaml"
    shutil.copy(REAL_PROFILE, path)
    return path


def test_noop_round_trip_is_byte_identical(profile_path):
    original = profile_path.read_text()
    profile_io.save_round_trip(profile_io.load_round_trip(profile_path), profile_path)
    assert profile_path.read_text() == original


def test_scalar_edit_preserves_comments(profile_path):
    doc = profile_io.load_round_trip(profile_path)
    profile_io.apply_edits(doc, {"gates.max_posting_age_days": 45})
    profile_io.save_round_trip(doc, profile_path)

    text = profile_path.read_text()
    assert "max_posting_age_days: 45" in text
    for comment in SENTINEL_COMMENTS:
        assert comment in text
    assert yaml.safe_load(text)["gates"]["max_posting_age_days"] == 45


def test_list_edit_is_minimal(profile_path):
    doc = profile_io.load_round_trip(profile_path)
    keywords = list(profile_io.get_value(doc, "scoring.preferred_keywords"))
    keywords.remove("kafka")
    keywords.append("terraform")
    profile_io.apply_edits(doc, {"scoring.preferred_keywords": keywords})
    profile_io.save_round_trip(doc, profile_path)

    text = profile_path.read_text()
    loaded = yaml.safe_load(text)["scoring"]["preferred_keywords"]
    assert "kafka" not in loaded
    assert loaded[-1] == "terraform"  # new items append, survivors keep position
    assert loaded[0] == "next.js"
    for comment in SENTINEL_COMMENTS:
        assert comment in text
    # Inline comment on an untouched allowed_locations item survives too.
    assert "# 'Remote - U.S.A.'" in text


def test_untouched_sections_stay_identical(profile_path):
    original = profile_path.read_text()
    doc = profile_io.load_round_trip(profile_path)
    profile_io.apply_edits(doc, {"scoring.title_match_weight": 35})
    profile_io.save_round_trip(doc, profile_path)

    text = profile_path.read_text()

    def section(source: str, start: str, end: str | None = None) -> str:
        return source[source.index(start) : source.index(end) if end else None]

    assert section(text, "companies:", "gates:") == section(original, "companies:", "gates:")
    assert section(text, "scraper:") == section(original, "scraper:")


def test_apply_edits_rejects_non_whitelisted_path(profile_path):
    doc = profile_io.load_round_trip(profile_path)
    with pytest.raises(ValueError, match="not editable"):
        profile_io.apply_edits(doc, {"scraper.delay_seconds": 0})


def test_parse_form_types():
    form = {path: "" for path in profile_io.FIELDS}
    form.update(
        {
            "gates.max_posting_age_days": "30",
            "gates.work_type.mine": "hybrid",
            "scoring.seniority.target": "mid",
            "scoring.excluded": "ignored-unknown-key",
            "gates.excluded_title_keywords": "  recruiter \n\n staffing ",
            "scoring.sparse_description.multiplier": "0.85",
            "companies": "Acme | greenhouse | acme\n Beta|lever|beta \n\nAcme | greenhouse | acme",
        }
    )
    for path, kind in profile_io.FIELDS.items():
        if not form[path] and kind in ("int", "float"):
            form[path] = "1"

    edits = profile_io.parse_form(form)
    # Companies parse to entries; whitespace tolerated, duplicates keep first.
    assert edits["companies"] == [
        {"name": "Acme", "ats": "greenhouse", "slug": "acme"},
        {"name": "Beta", "ats": "lever", "slug": "beta"},
    ]
    assert edits["gates.excluded_title_keywords"] == ["recruiter", "staffing"]
    assert edits["gates.max_posting_age_days"] == 30
    assert edits["scoring.sparse_description.multiplier"] == 0.85
    # Checkbox absent from the form reads as False.
    assert edits["gates.work_type.reject_conflicts"] is False
    assert "scoring.excluded" not in edits


def test_parse_form_rejects_bad_values():
    good = {path: "1" for path in profile_io.FIELDS}
    good["gates.work_type.mine"] = "hybrid"
    good["scoring.seniority.target"] = "mid"
    good["companies"] = "Acme | greenhouse | acme"

    bad_number = dict(good, **{"gates.max_posting_age_days": "abc"})
    with pytest.raises(ValueError, match="not a number"):
        profile_io.parse_form(bad_number)

    bad_choice = dict(good, **{"gates.work_type.mine": "hybrid; rm -rf"})
    with pytest.raises(ValueError, match="not one of"):
        profile_io.parse_form(bad_choice)

    bad_shape = dict(good, **{"companies": "Acme greenhouse acme"})
    with pytest.raises(ValueError, match="Name | ats | slug"):
        profile_io.parse_form(bad_shape)

    bad_ats = dict(good, **{"companies": "Acme | workday | acme"})
    with pytest.raises(ValueError, match="not one of"):
        profile_io.parse_form(bad_ats)


def test_companies_sync_add_remove_rename(profile_path):
    doc = profile_io.load_round_trip(profile_path)
    entries = [
        {"name": e["name"], "ats": e["ats"], "slug": e["slug"]}
        for e in profile_io.get_value(doc, "companies")
    ]
    entries = [e for e in entries if e["slug"] != "pinterest"]  # remove (not first — see below)
    for entry in entries:
        if entry["slug"] == "mongodb":
            entry["name"] = "MongoDB Inc"  # rename in place
    # A name/slug that can never be on the real watchlist (the fixture
    # copies the live profile.yaml, so a real company here would collide).
    entries.append({"name": "Fake Testco", "ats": "ashby", "slug": "fake-testco"})  # add

    profile_io.apply_edits(doc, {"companies": entries})
    profile_io.save_round_trip(doc, profile_path)

    text = profile_path.read_text()
    loaded = yaml.safe_load(text)["companies"]
    slugs = [e["slug"] for e in loaded]
    assert "pinterest" not in slugs
    assert slugs[-1] == "fake-testco"  # new entries append, survivors keep position
    assert next(e for e in loaded if e["slug"] == "mongodb")["name"] == "MongoDB Inc"
    # Section comments survive (attached to untouched neighbors).
    assert "# --- Lever ---" in text
    assert "# --- Ashby ---" in text
    for comment in SENTINEL_COMMENTS:
        assert comment in text


def test_companies_weight_field_round_trip(profile_path):
    doc = profile_io.load_round_trip(profile_path)
    entries = [dict(e) for e in profile_io.get_value(doc, "companies")]
    for entry in entries:
        if entry["slug"] == "notion":
            entry["weight"] = 0.8
    profile_io.apply_edits(doc, {"companies": entries})
    profile_io.save_round_trip(doc, profile_path)

    loaded = yaml.safe_load(profile_path.read_text())["companies"]
    assert next(e for e in loaded if e["slug"] == "notion")["weight"] == 0.8

    # Dropping the 4th field removes the key again.
    doc = profile_io.load_round_trip(profile_path)
    entries = [
        {"name": e["name"], "ats": e["ats"], "slug": e["slug"]}
        for e in profile_io.get_value(doc, "companies")
    ]
    profile_io.apply_edits(doc, {"companies": entries})
    profile_io.save_round_trip(doc, profile_path)
    loaded = yaml.safe_load(profile_path.read_text())["companies"]
    assert all("weight" not in e for e in loaded)


def test_parse_companies_weight():
    parsed = profile_io._parse_companies("Cedar | ashby | cedar | 0.8\nBig Co | lever | bigco | 1")
    assert parsed[0]["weight"] == 0.8
    assert "weight" not in parsed[1]  # 1.0 is the default, not stored
    with pytest.raises(ValueError, match="not a number"):
        profile_io._parse_companies("Cedar | ashby | cedar | heavy")
    with pytest.raises(ValueError, match="must be in"):
        profile_io._parse_companies("Cedar | ashby | cedar | 0")
