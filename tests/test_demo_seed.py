"""The demo's synthetic database: deterministic, self-contained and alive.

Builds use a fixed anchor day in the past, so anything stamped with the
real clock shows up as a difference.
"""

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone

import pytest
import yaml

import db
import demo_seed
from scoring.score import score_job

ANCHOR = date(2026, 3, 11)  # a Wednesday


def _open(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _dump(path) -> str:
    conn = _open(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    path = tmp_path_factory.mktemp("seed") / "seed.db"
    summary = demo_seed.build(path, ANCHOR, slot=2)
    return path, summary


@pytest.fixture()
def conn(built):
    connection = _open(built[0])
    yield connection
    connection.close()


# --- where it writes ---------------------------------------------------------

def test_refuses_the_real_database(tmp_path, monkeypatch):
    real = tmp_path / "jobs.db"
    real.write_bytes(b"the real database")
    monkeypatch.setattr(db, "DB_PATH", real)
    with pytest.raises(ValueError, match="never writes to the real database"):
        demo_seed.build(real, ANCHOR)
    assert real.read_bytes() == b"the real database"


def test_opens_no_database_but_its_own(tmp_path, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def recording_connect(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", recording_connect)
    out = tmp_path / "seed.db"
    demo_seed.build(out, ANCHOR)
    # One scratch file per process, so two workers can build at once.
    assert opened == [f"{out}.{os.getpid()}.building"]
    assert [p.name for p in tmp_path.iterdir()] == ["seed.db"]


# --- determinism -------------------------------------------------------------

def test_same_anchor_gives_the_same_database(built, tmp_path):
    again = tmp_path / "again.db"
    demo_seed.build(again, ANCHOR, slot=2)
    assert _dump(again) == _dump(built[0])


def test_nothing_is_stamped_with_the_real_clock(built):
    dump = _dump(built[0])
    assert date.today().isoformat() not in dump
    assert datetime.now(timezone.utc).date().isoformat() not in dump


def test_last_sweep_is_never_in_the_future(tmp_path):
    for slot in (0, 1, 2):
        path = tmp_path / f"slot{slot}.db"
        demo_seed.build(path, ANCHOR, slot)
        clock = demo_seed.Clock(ANCHOR, slot)
        conn = _open(path)
        newest = conn.execute(
            "SELECT MAX(last_seen), MAX(first_seen) FROM jobs"
        ).fetchone()
        events = conn.execute("SELECT MAX(occurred_at) FROM events").fetchone()[0]
        conn.close()
        limit = clock.utc(clock.ref)
        assert max(newest) <= limit
        if slot == 0:   # before the morning sweep nothing has happened today
            assert events < limit


def test_slot_for_follows_the_sweep_times():
    day = datetime(2026, 3, 11)
    assert demo_seed.slot_for(day.replace(hour=7)) == 0
    assert demo_seed.slot_for(day.replace(hour=8, minute=35)) == 0
    assert demo_seed.slot_for(day.replace(hour=8, minute=36)) == 1
    assert demo_seed.slot_for(day.replace(hour=16, minute=36)) == 2


def test_sweep_times_match_the_app():
    import app

    assert [h * 60 + m for h, m in demo_seed.SWEEPS] == [at for at, _ in app.SWEEP_TIMES]


# --- content -----------------------------------------------------------------

@pytest.mark.parametrize("anchor", [
    date(2026, 3, 9),    # Monday
    date(2026, 3, 13),   # Friday
    date(2026, 3, 14),   # Saturday
    date(2026, 3, 15),   # Sunday
])
@pytest.mark.parametrize("slot", [0, 2])
def test_every_lane_has_cards_on_any_day(tmp_path, anchor, slot):
    path = tmp_path / "seed.db"
    demo_seed.build(path, anchor, slot)
    conn = _open(path)
    lanes = {row[0] for row in conn.execute("SELECT DISTINCT lane FROM applications")}
    order_ok = conn.execute(
        """SELECT COUNT(*) FROM events a JOIN events b
           ON a.application_id = b.application_id AND a.id < b.id
           WHERE a.occurred_at > b.occurred_at"""
    ).fetchone()[0]
    conn.close()
    assert lanes == set(db.LANES)
    assert order_ok == 0, "an application's events are out of order"


def test_every_usable_gate_is_represented(conn):
    gates = set()
    for row in conn.execute("SELECT gate_reason FROM jobs WHERE gate_failed = 1"):
        gates |= {part.split(":")[0] for part in row["gate_reason"].split("; ")}
    assert gates >= {"location", "excluded-title", "experience", "clearance", "delisted"}


def test_the_feed_has_a_real_spread(conn):
    scores = [row[0] for row in conn.execute(
        "SELECT score FROM jobs WHERE gate_failed = 0 AND dismissed = 0")]
    assert len(scores) > 100
    assert max(scores) >= 60 and min(scores) < 10
    assert sum(1 for s in scores if 30 <= s < 60) > 30


def test_scores_come_from_the_real_scorer(conn):
    profile = yaml.safe_load(demo_seed.PROFILE_PATH.read_text())
    weights = {(c["ats"], c["slug"]): c["weight"]
               for c in profile["companies"] if "weight" in c}
    rows = conn.execute(
        """SELECT j.title, j.location, j.description, j.score, j.score_breakdown,
                  c.ats_type, c.slug
           FROM jobs j JOIN companies c ON c.id = j.company_id
           WHERE j.gate_failed = 0 ORDER BY j.id LIMIT 40"""
    ).fetchall()
    for row in rows:
        job = {"title": row["title"], "location": row["location"],
               "description": row["description"],
               "company_weight": weights.get((row["ats_type"], row["slug"]))}
        result = score_job(job, profile["scoring"])
        assert result.score == row["score"]
        assert result.breakdown() == json.loads(row["score_breakdown"])


def test_mail_labels_agree_with_the_classifier(built):
    results = built[1]["mail"]
    assert len(results) >= 35

    def agrees(result):
        expect = result["expect"]
        return expect["kind"] == result["kind"] and expect.get("app") in (None, result["app"])

    wrong = [r["id"] for r in results if not r["known_miss"] and not agrees(r)]
    assert wrong == [], f"classifier disagrees with the label: {wrong}"
    stale = [r["id"] for r in results if r["known_miss"] and agrees(r)]
    assert stale == [], f"marked known_miss but the classifier now agrees: {stale}"


def test_tracker_and_mail_tell_one_story(conn):
    """An accepted lane suggestion is always followed by that lane move."""
    rows = conn.execute(
        """SELECT s.application_id, s.suggested_lane, s.received_at
           FROM email_suggestions s
           WHERE s.status = 'accepted' AND s.suggested_lane IS NOT NULL"""
    ).fetchall()
    assert rows
    for row in rows:
        moved = conn.execute(
            """SELECT 1 FROM events WHERE application_id = ? AND type = 'lane_change'
               AND detail LIKE ? AND occurred_at > ?""",
            (row["application_id"], f"% -> {row['suggested_lane']}", row["received_at"]),
        ).fetchone()
        assert moved, f"no move to {row['suggested_lane']} after its mail"


def test_contacts_cover_every_state_the_pages_show(conn):
    rows = conn.execute("SELECT * FROM contacts").fetchall()
    live = [r for r in rows if r["removed_at"] is None]
    assert {r["track"] for r in live} >= {"referral", "mentor", "agency", "company_recruiter"}
    assert any(r["standing_referral"] for r in live)
    assert any(r["new_message"] for r in live)
    assert any(r["status"] == "Skip" for r in live)
    assert any(r["priority"] == "C" for r in live)
    assert len(rows) > len(live), "one removed contact stays on file"
    assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] > 30
    assert conn.execute("SELECT COUNT(*) FROM contact_applications").fetchone()[0] >= 3


def test_schedule_has_history(conn):
    assert conn.execute("SELECT COUNT(*) FROM day_log").fetchone()[0] >= 8
    assert conn.execute("SELECT COUNT(*) FROM day_excused").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sweep_runs").fetchone()[0] == 28
    assert conn.execute(
        "SELECT COUNT(*) FROM sweep_runs WHERE errors IS NOT NULL").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE dismissed = 1").fetchone()[0] >= 10


# --- nothing points at a real person or site -------------------------------

_ADDRESS = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")


def test_links_and_addresses_use_reserved_names(built):
    conn = _open(built[0])
    urls = [row[0] for row in conn.execute("SELECT url FROM jobs")]
    conn.close()
    assert all(url.startswith("https://example.com/") for url in urls)
    domains = set(_ADDRESS.findall(_dump(built[0])))
    for path in demo_seed.DATA_DIR.rglob("*"):
        if path.is_file():
            domains |= set(_ADDRESS.findall(path.read_text()))
    assert domains, "expected some addresses to check"
    stray = {d for d in domains if not (d == "example.com" or d.endswith(".example"))}
    assert stray == set()
