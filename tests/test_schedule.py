"""Daily schedule: pure schedule.py rules, the /schedule routes, and the
connector's today_plan/log_block tools.

Route tests follow test_tracker.py (TestClient, get_db overridden to a tmp
database). The clock is pinned by passing `now` to the pure functions; the
routes read the real clock, so they only assert on things time can't change.
"""

import asyncio
import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import db
import mcp_server
import schedule
from app import app, get_db

CFG = {
    "workdays": ["mon", "tue", "wed", "thu", "fri"],
    "blocks": [
        {"key": "triage", "label": "Triage", "start": "09:00", "end": "09:30", "kind": "triage"},
        {"key": "outreach", "label": "Outreach", "start": "09:30", "end": "10:30",
         "kind": "outreach", "target": 3},
        {"key": "applications", "label": "Applications", "start": "10:30", "end": "12:30",
         "kind": "applications", "target": 2},
        {"key": "coding", "label": "Coding prep", "start": "13:30", "end": "15:00", "kind": "manual"},
        {"key": "prep", "label": "Hello Interview", "start": "15:15", "end": "16:30",
         "kind": "manual", "detail": "One topic.", "themed": True},
        {"key": "shutdown", "label": "Shut down", "start": "16:30", "end": "16:45", "kind": "manual"},
    ],
    "themes": {"mon": "Learn a concept.", "tue": "Full design problem."},
    "overrides": {
        "fri": {
            "applications": {"target": 1},
            "coding": {"label": "Mock interview"},
            "prep": {"label": "Weekly review", "end": "15:45", "themed": False},
            "shutdown": {"drop": True},
        }
    },
    "weekly_targets": {"applications": 9, "outreach_new": 15, "conversations": 2,
                       "coding": 4, "prep": 4},
}

MON = date(2026, 11, 9)   # a Monday
TUE = date(2026, 11, 10)
FRI = date(2026, 11, 13)
SAT = date(2026, 11, 14)
EMPTY = {"lane_moves": [], "dismissed": {}, "interactions": [], "sweeps": []}


def at(day, hhmm):
    hour, minute = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(hour), int(minute))


def move(day, app_id, detail, title="SE II", company="Acme"):
    return {"day": day.isoformat(), "application_id": app_id, "detail": detail,
            "at": "11:00:00", "title": title, "company": company}


def touch(day, contact_id, direction="out", first=True, name="Pat", company="Globex"):
    return {"day": day.isoformat(), "direction": direction, "channel": "email",
            "summary": "hi", "contact_id": contact_id, "name": name,
            "company": company, "track": "company_recruiter", "first_touch": int(first)}


class TestDayPlan:
    def test_weekday_gets_every_block_in_order(self):
        keys = [b["key"] for b in schedule.day_plan(CFG, MON)]
        assert keys == ["triage", "outreach", "applications", "coding", "prep", "shutdown"]

    def test_theme_replaces_detail_on_themed_block(self):
        prep = {b["key"]: b for b in schedule.day_plan(CFG, TUE)}["prep"]
        assert prep["detail"] == "Full design problem."

    def test_unthemed_weekday_keeps_base_detail(self):
        prep = {b["key"]: b for b in schedule.day_plan(CFG, date(2026, 11, 12))}["prep"]
        assert prep["detail"] == "One topic."

    def test_friday_overrides_apply_and_dropped_block_goes(self):
        plan = {b["key"]: b for b in schedule.day_plan(CFG, FRI)}
        assert "shutdown" not in plan
        assert plan["applications"]["target"] == 1
        assert plan["coding"]["label"] == "Mock interview"
        assert plan["prep"]["end"] == "15:45"
        assert plan["prep"]["detail"] == "One topic."  # themed turned off

    def test_rest_day_has_no_blocks(self):
        assert schedule.day_plan(CFG, SAT) == []
        assert not schedule.is_workday(CFG, SAT)

    def test_manual_default_kind_and_target(self):
        cfg = {"blocks": [{"key": "x", "label": "X", "start": "09:00", "end": "10:00"}]}
        block = schedule.day_plan(cfg, MON)[0]
        assert block["kind"] == "manual" and block["target"] == 1


class TestStatus:
    def _statuses(self, now, activity=EMPTY, logs=()):
        built = schedule.build_day(CFG, MON, activity, list(logs), now)
        return {b["key"]: b["status"] for b in built["blocks"]}

    def test_clock_splits_the_day(self):
        s = self._statuses(at(MON, "10:00"))
        assert s["triage"] == "missed"
        assert s["outreach"] == "now"
        assert s["applications"] == "upcoming"
        assert s["shutdown"] == "upcoming"

    def test_applications_count_decisions_not_starts(self):
        activity = {**EMPTY, "lane_moves": [
            move(MON, 1, "considering"),            # tracked = started, not decided
            move(MON, 2, "considering"),            # tracked then decided: shows once
            move(MON, 2, "considering -> applied"),
            move(MON, 3, "ready -> skipped", title="Platform"),
        ]}
        built = schedule.build_day(CFG, MON, activity, [], at(MON, "13:00"))
        apps = {b["key"]: b for b in built["blocks"]}["applications"]
        assert apps["status"] == "done" and apps["count"] == 2
        assert apps["evidence"] == [
            "Acme — SE II: applied", "Acme — Platform: skipped", "Acme — SE II: started",
        ]

    def test_one_of_two_applications_is_partial_once_over(self):
        activity = {**EMPTY, "lane_moves": [move(MON, 2, "considering -> applied")]}
        assert self._statuses(at(MON, "13:00"), activity)["applications"] == "partial"
        # Still inside the block, it reads as in progress rather than short.
        assert self._statuses(at(MON, "11:00"), activity)["applications"] == "now"

    def test_same_application_moved_twice_counts_once(self):
        activity = {**EMPTY, "lane_moves": [
            move(MON, 2, "considering -> ready"), move(MON, 2, "ready -> applied"),
        ]}
        built = schedule.build_day(CFG, MON, activity, [], at(MON, "13:00"))
        assert {b["key"]: b for b in built["blocks"]}["applications"]["count"] == 1

    def test_outreach_counts_new_and_follow_ups(self):
        activity = {**EMPTY, "interactions": [
            touch(MON, 1), touch(MON, 2, first=False, name="Sam"),
            touch(MON, 3, direction="in"),           # a reply isn't outreach
        ]}
        built = schedule.build_day(CFG, MON, activity, [], at(MON, "11:00"))
        out = {b["key"]: b for b in built["blocks"]}["outreach"]
        assert out["count"] == 2
        assert out["evidence"] == ["Pat at Globex: new message", "Sam at Globex: follow-up"]

    def test_triage_sees_dismissals_and_sweep(self):
        activity = {**EMPTY, "dismissed": {MON.isoformat(): 3},
                    "sweeps": [{"day": MON.isoformat(), "at": "08:31:04"}]}
        built = schedule.build_day(CFG, MON, activity, [], at(MON, "12:00"))
        triage = {b["key"]: b for b in built["blocks"]}["triage"]
        assert triage["status"] == "done"
        assert triage["evidence"] == ["3 jobs dismissed", "Sweep ran 08:31"]

    def test_sweep_alone_is_not_triage(self):
        activity = {**EMPTY, "sweeps": [{"day": MON.isoformat(), "at": "08:31:04"}]}
        assert self._statuses(at(MON, "12:00"), activity)["triage"] == "missed"

    def test_log_marks_any_block_done(self):
        logs = [{"day": MON.isoformat(), "block_key": "coding", "note": "graphs", "source": "claude"},
                {"day": MON.isoformat(), "block_key": "outreach", "note": None, "source": "ui"}]
        built = schedule.build_day(CFG, MON, EMPTY, logs, at(MON, "16:00"))
        by = {b["key"]: b for b in built["blocks"]}
        assert by["coding"]["status"] == "done" and by["coding"]["note"] == "graphs"
        assert by["coding"]["logged_by"] == "claude"
        assert by["outreach"]["status"] == "done"
        assert by["prep"]["status"] == "now"

    def test_other_days_logs_do_not_bleed(self):
        logs = [{"day": TUE.isoformat(), "block_key": "coding", "note": None, "source": "ui"}]
        assert self._statuses(at(MON, "16:00"), logs=logs)["coding"] == "missed"

    def test_future_day_is_planned(self):
        built = schedule.build_day(CFG, TUE, EMPTY, [], at(MON, "16:00"))
        assert {b["status"] for b in built["blocks"]} == {"planned"}
        assert built["adherence"]["pct"] is None

    def test_past_day_ignores_the_clock(self):
        built = schedule.build_day(CFG, MON, EMPTY, [], at(TUE, "09:10"))
        assert {b["status"] for b in built["blocks"]} == {"missed"}


class TestAdherence:
    def test_partial_counts_half_over_elapsed_blocks(self):
        blocks = [{"status": "done"}, {"status": "partial"}, {"status": "missed"},
                  {"status": "upcoming"}]
        a = schedule.adherence(blocks)
        assert a == {"done": 1, "partial": 1, "elapsed": 3, "planned": 4, "pct": 50}

    def test_nothing_elapsed_is_undefined(self):
        assert schedule.adherence([{"status": "upcoming"}])["pct"] is None

    def test_next_block_prefers_the_one_in_progress(self):
        blocks = [{"key": "a", "status": "missed"}, {"key": "b", "status": "upcoming"},
                  {"key": "c", "status": "now"}]
        assert schedule.next_block(blocks)["key"] == "c"
        assert schedule.next_block(blocks[:2])["key"] == "b"
        assert schedule.next_block([{"key": "a", "status": "done"}]) is None


class TestWeek:
    def test_targets_and_strip(self):
        activity = {
            "lane_moves": [move(MON, 1, "considering -> applied"),
                           move(TUE, 1, "applied -> interviewing"),   # same app, no double count
                           move(TUE, 2, "considering -> ready"),
                           move(TUE, 3, "considering -> skipped")],   # skip is decided, not applied
            "dismissed": {},
            "interactions": [touch(MON, 1), touch(TUE, 1, first=False),
                             touch(TUE, 2, direction="in"), touch(TUE, 2, direction="in")],
            "sweeps": [],
        }
        logs = [{"day": MON.isoformat(), "block_key": "coding", "note": None, "source": "ui"}]
        week = schedule.build_week(CFG, TUE, activity, logs, at(TUE, "17:00"))
        by = {t["key"]: t for t in week["targets"]}
        assert by["applications"]["done"] == 2
        assert by["outreach_new"]["done"] == 1
        assert by["conversations"]["done"] == 1
        assert by["coding"]["done"] == 1 and by["coding"]["label"] == "Coding prep"
        assert by["prep"]["done"] == 0
        assert by["applications"]["pct"] == 22
        assert [d["weekday"] for d in week["days"]] == ["mon", "tue", "wed", "thu", "fri"]
        assert week["days"][1]["is_today"] and week["days"][2]["future"]
        assert week["days"][2]["pct"] is None

    def test_week_pct_averages_scored_days(self):
        logs = [{"day": MON.isoformat(), "block_key": k, "note": None, "source": "ui"}
                for k in ("triage", "outreach", "applications", "coding", "prep", "shutdown")]
        week = schedule.build_week(CFG, TUE, EMPTY, logs, at(TUE, "17:00"))
        assert week["days"][0]["pct"] == 100 and week["days"][1]["pct"] == 0
        assert week["pct"] == 50

    def test_target_pct_caps_at_100(self):
        logs = [{"day": MON.isoformat(), "block_key": "coding", "note": None, "source": "ui"}] * 6
        week = schedule.build_week(CFG, MON, EMPTY, logs, at(MON, "17:00"))
        assert {t["key"]: t for t in week["targets"]}["coding"]["pct"] == 100


# --- database -----------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(tmp_path / "t.db")
    db.init_db(c)
    yield c
    c.close()


class TestDayLog:
    def test_log_twice_appends_note_and_keeps_one_row(self, conn):
        db.log_block(conn, "2026-11-09", "coding", "graphs", "ui")
        db.log_block(conn, "2026-11-09", "coding", "then DP", "claude")
        db.log_block(conn, "2026-11-09", "coding", None, "ui")
        rows = db.fetch_day_logs(conn, "2026-11-09", "2026-11-09")
        assert len(rows) == 1
        assert rows[0]["note"] == "graphs\nthen DP"
        assert rows[0]["source"] == "ui"

    def test_clear_removes_only_that_day(self, conn):
        db.log_block(conn, "2026-11-09", "coding", None, "ui")
        db.log_block(conn, "2026-11-10", "coding", None, "ui")
        assert db.clear_block(conn, "2026-11-09", "coding")
        assert not db.clear_block(conn, "2026-11-09", "coding")
        assert [r["day"] for r in db.fetch_day_logs(conn, "2026-11-01", "2026-11-30")] == ["2026-11-10"]

    def test_activity_buckets_by_local_day(self, conn):
        conn.execute("INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')")
        job = conn.execute(
            "INSERT INTO jobs (company_id, external_id, title, location, url) VALUES (1, 'x', 'SE II', 'NYC', 'u')"
        ).lastrowid
        app_id = db.create_application(conn, job)
        db.set_application_lane(conn, app_id, "applied")
        today = date.today().isoformat()
        activity = db.fetch_schedule_activity(conn, today, today)
        details = [m["detail"] for m in activity["lane_moves"]]
        assert details == ["considering", "considering -> applied"]
        assert activity["lane_moves"][0]["company"] == "Acme"
        cid = db.insert_contact(conn, name="Pat", company="Globex", track="company_recruiter",
                                source="research", date_contacted=today)
        db.add_interaction(conn, cid, today, "email", "out", "sent")
        activity = db.fetch_schedule_activity(conn, today, today)
        assert activity["interactions"][0]["first_touch"] == 1


# --- routes -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test.db"
    conn = db.get_connection(db_path)
    db.init_db(conn)
    client = TestClient(app)
    client.db = conn

    def override():
        request_conn = db.get_connection(db_path, check_same_thread=False)
        try:
            yield request_conn
        finally:
            request_conn.close()

    app.dependency_overrides[get_db] = override
    yield client
    app.dependency_overrides.clear()
    conn.close()


class TestRoutes:
    def test_page_renders_a_workday(self, client):
        r = client.get("/schedule?day=2026-11-09")
        assert r.status_code == 200
        assert "Monday, Nov 9" in r.text
        assert "Outreach and referrals" in r.text
        assert "Week of 11/09" in r.text

    def test_friday_drops_shutdown_and_sunday_rests(self, client):
        fri = client.get("/schedule?day=2026-11-13").text
        assert "Mock interview" in fri and "Shut down" not in fri
        sun = client.get("/schedule?day=2026-11-15").text
        assert "Rest day" in sun

    def test_bad_day_is_400_and_bad_key_404(self, client):
        assert client.get("/schedule?day=yesterday").status_code == 400
        r = client.post("/schedule/log", data={"day": "2026-11-09", "key": "nap"})
        assert r.status_code == 404

    def test_log_then_clear_round_trip(self, client):
        r = client.post("/schedule/log", data={"day": "2026-11-09", "key": "coding", "note": "  graphs "})
        assert r.status_code == 200
        assert "Marked done: graphs" in r.text
        assert "Undo" in r.text
        row = client.db.execute("SELECT note, source FROM day_log").fetchone()
        assert (row["note"], row["source"]) == ("graphs", "ui")
        r = client.post("/schedule/clear", data={"day": "2026-11-09", "key": "coding"})
        assert "Marked done" not in r.text
        assert client.db.execute("SELECT COUNT(*) FROM day_log").fetchone()[0] == 0

    def test_nav_has_the_tab(self, client):
        assert 'href="/schedule"' in client.get("/tracker").text


# --- connector ----------------------------------------------------------------


@pytest.fixture
def mcp_db(tmp_path, monkeypatch):
    path = tmp_path / "mcp.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.get_connection(path)
    db.init_db(conn)
    yield conn
    conn.close()


def call(name, **args):
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


class TestConnector:
    def test_today_plan_lists_blocks(self, mcp_db):
        plan = call("today_plan", day="2026-11-10")
        assert plan["day"] == "2026-11-10" and plan["workday"]
        assert [b["key"] for b in plan["blocks"]][:3] == ["triage", "outreach", "applications"]
        assert {t["key"] for t in plan["week"]["targets"]} >= {"applications", "outreach_new"}

    def test_log_block_marks_done_from_claude(self, mcp_db):
        plan = call("log_block", block_key="prep", note="URL shortener", day="2026-11-10")
        prep = {b["key"]: b for b in plan["blocks"]}["prep"]
        assert prep["status"] == "done" and prep["note"] == "URL shortener"
        row = mcp_db.execute("SELECT source FROM day_log").fetchone()
        assert row["source"] == "claude"

    def test_unknown_block_is_refused_with_reason(self, mcp_db):
        with pytest.raises(Exception) as err:
            asyncio.run(mcp_server.mcp.call_tool("log_block", {"block_key": "nap"}))
        assert "no block 'nap'" in str(err.value)
