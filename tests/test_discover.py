"""Company discovery: slug candidates, board probing, and profile append."""

import shutil
from pathlib import Path

import pytest
import yaml

import profile_io
from scrapers import ashby, common, discover, greenhouse, lever

REAL_PROFILE = Path(__file__).parent.parent / "config" / "profile.yaml"


@pytest.mark.parametrize(
    "name, expected_first",
    [
        ("FanDuel", "fanduel"),
        ("Warby Parker", "warbyparker"),
        ("Harry's", "harrys"),
        ("Spring Health", "springhealth"),
        ("Cockroach Labs", "cockroachlabs"),
    ],
)
def test_slug_candidates_first_guess(name, expected_first):
    assert discover.slug_candidates(name)[0] == expected_first


def test_slug_candidates_variants():
    candidates = discover.slug_candidates("Spring Health")
    assert "spring-health" in candidates  # hyphenated variant
    assert "spring" in candidates  # bare first word, tried last
    assert candidates.index("springhealth") < candidates.index("spring")


def test_slug_candidates_tries_glued_inc_suffix():
    # StubHub's board token is "stubhubinc"; a name that already ends in
    # a suffix word doesn't get a second one.
    assert discover.slug_candidates("StubHub")[-1] == "stubhubinc"
    assert "acmeincinc" not in discover.slug_candidates("Acme Inc")


def _not_found(slug, **kw):
    raise common.BoardNotFound(f"404 for {slug}")


def test_probe_finds_first_answering_board(monkeypatch):
    monkeypatch.setattr(greenhouse, "fetch", _not_found)
    monkeypatch.setattr(
        lever, "fetch",
        lambda slug, **kw: [{"id": "x", "text": "Engineer"}] if slug == "acme" else _not_found(slug),
    )
    monkeypatch.setattr(ashby, "fetch", _not_found)
    hit = discover.probe_company("Acme", user_agent="t", timeout=5, delay=0)
    assert hit == {"name": "Acme", "ats": "lever", "slug": "acme", "jobs": 1}


def test_probe_rejects_empty_boards(monkeypatch):
    # An empty board is indistinguishable from a wrong slug — not a hit.
    monkeypatch.setattr(greenhouse, "fetch", lambda slug, **kw: {"jobs": []})
    monkeypatch.setattr(lever, "fetch", lambda slug, **kw: [])
    monkeypatch.setattr(ashby, "fetch", _not_found)
    assert discover.probe_company("Ghost Co", user_agent="t", timeout=5, delay=0) is None


def test_main_add_appends_to_profile(tmp_path, monkeypatch, capsys):
    profile_path = tmp_path / "profile.yaml"
    shutil.copy(REAL_PROFILE, profile_path)
    monkeypatch.setattr(profile_io, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(
        discover, "probe_company",
        lambda name, **kw: {"name": name, "ats": "lever", "slug": "acme", "jobs": 7},
    )

    discover.main(["--add", "Acme", "FanDuel"])  # FanDuel already watched

    out = capsys.readouterr().out
    assert "found  | Acme" in out
    assert "skip   | FanDuel" in out
    loaded = yaml.safe_load(profile_path.read_text())["companies"]
    assert loaded[-1] == {"name": "Acme", "ats": "lever", "slug": "acme"}
    assert "# --- Lever ---" in profile_path.read_text()  # comments survive


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    profile_path = tmp_path / "profile.yaml"
    shutil.copy(REAL_PROFILE, profile_path)
    original = profile_path.read_text()
    monkeypatch.setattr(profile_io, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(
        discover, "probe_company",
        lambda name, **kw: {"name": name, "ats": "lever", "slug": "acme", "jobs": 7},
    )

    discover.main(["Acme"])

    assert "Re-run with --add" in capsys.readouterr().out
    assert profile_path.read_text() == original
