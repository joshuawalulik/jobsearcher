"""Demo sandboxes: a visitor's changes go to a private copy of the seed.

Reading never creates anything. The first change copies the seed into a
file tied to a cookie; from then on that visitor reads and writes their own
copy, nobody else sees it, and it is deleted when it goes unused.
"""

import hashlib
import logging
import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db
import demo
from app import app

T0 = 1_800_000_000.0  # a fixed "now" for the rules that take one


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("sandbox-demo")


@pytest.fixture()
def demo_on(demo_dir, monkeypatch):
    """Demo mode on a private folder, with no sandboxes left from other tests."""
    monkeypatch.setenv("JOBSEARCHER_DEMO_DIR", str(demo_dir))
    monkeypatch.setattr(demo, "ENABLED", True)
    monkeypatch.setattr(demo, "_started", {})
    monkeypatch.setattr(demo, "_last_tidy", 0.0)
    monkeypatch.setattr(app_module, "PROFILE_PATH", demo.PROFILE_PATH)
    monkeypatch.setattr(app_module, "CONVERSATIONS_DIR", demo.CONVERSATIONS_DIR)
    real_path, real_get = db.DB_PATH, db.get_connection

    def guarded(db_path=None, **kwargs):
        assert db_path is not None and Path(db_path) != real_path, (
            "demo mode opened the real database")
        return real_get(db_path, **kwargs)

    monkeypatch.setattr(db, "get_connection", guarded)
    for leftover in demo.sandbox_dir().glob("*"):
        leftover.unlink()
    return demo_dir


def _visitor(base_url="http://localhost", **kwargs) -> TestClient:
    return TestClient(app, base_url=base_url, **kwargs)


@pytest.fixture()
def client(demo_on):
    return _visitor()


def _sandboxes() -> list[Path]:
    folder = demo.sandbox_dir()
    return sorted(folder.glob("*.db")) if folder.is_dir() else []


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ids() -> dict:
    conn = demo.connect()
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    found = {
        "job": one(
            "SELECT id FROM jobs WHERE gate_failed = 0 AND dismissed = 0 AND score IS NOT NULL"
            " AND id NOT IN (SELECT job_id FROM applications) ORDER BY score DESC LIMIT 1"),
        "app": one("SELECT id FROM applications WHERE lane = 'considering' LIMIT 1"),
        "suggestion": one(
            "SELECT id FROM email_suggestions WHERE status = 'pending'"
            " AND suggested_lane IS NOT NULL LIMIT 1"),
        "contact": one("SELECT id FROM contacts WHERE removed_at IS NULL LIMIT 1"),
    }
    # What the Dismissed page shows for that job and no other: its Restore button.
    found["restore"] = f'hx-post="/jobs/{found["job"]}/restore"'
    conn.close()
    return found


def _set_cookie(response) -> str:
    return response.headers.get("set-cookie", "")


# --- reading costs nothing ---------------------------------------------------


def test_reading_never_creates_a_sandbox(client):
    for path in ("/", "/tracker", "/contacts", "/schedule", "/analytics", "/dismissed"):
        response = client.get(path)
        assert response.status_code == 200
        assert not _set_cookie(response)
    assert _sandboxes() == []


def test_a_cookie_that_names_no_sandbox_reads_the_seed(client):
    made_up = f"{int(T0):08x}-{'0' * 32}"
    for value in ("x", "../../seed", "..%2f..%2fseed", made_up, made_up + ".db", ""):
        client.cookies.set(demo.COOKIE, value)
        assert client.get("/tracker").status_code == 200
    assert _sandboxes() == []


# --- the first change makes one ----------------------------------------------


def test_the_first_change_copies_the_seed_and_sets_the_cookie(client):
    ids, seed = _ids(), demo.ensure_seed()
    before = _digest(seed)
    response = client.post(f"/jobs/{ids['job']}/dismiss")
    assert response.status_code == 200
    made = _sandboxes()
    assert len(made) == 1
    cookie = _set_cookie(response)
    assert cookie.startswith(f"{demo.COOKIE}={made[0].stem};")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/" in lowered
    assert f"max-age={demo.MAX_AGE_SECONDS}" in lowered
    assert "secure" not in lowered  # plain http on the laptop
    assert _digest(seed) == before, "a visitor's change reached the shared seed"
    assert made[0].stat().st_mode & 0o077 == 0, "other accounts can read the sandbox"
    # The change is there for this visitor on the next page.
    assert ids["restore"] in client.get("/dismissed").text
    # And no second sandbox or cookie for the next change.
    again = client.post(f"/jobs/{ids['job']}/restore")
    assert again.status_code == 200 and not _set_cookie(again)
    assert len(_sandboxes()) == 1


def test_over_https_the_cookie_is_locked_to_the_host(demo_on):
    secure = _visitor(base_url="https://localhost")
    response = secure.post(f"/jobs/{_ids()['job']}/dismiss")
    cookie = _set_cookie(response)
    assert cookie.startswith(f"{demo.SECURE_COOKIE}=")
    assert "secure" in cookie.lower() and "path=/" in cookie.lower()
    assert "domain" not in cookie.lower()  # __Host- cookies must not name one
    assert _ids()["restore"] in secure.get("/dismissed").text


def test_a_visitor_can_use_the_app(client):
    ids = _ids()
    assert client.post(f"/jobs/{ids['job']}/track").status_code == 200
    assert client.post(
        f"/applications/{ids['app']}/lane", data={"lane": "applied"}).status_code == 200
    assert client.post(
        f"/applications/{ids['app']}", data={"notes": "sandbox note"}).status_code == 200
    assert client.post(f"/email/suggestions/{ids['suggestion']}/accept").status_code == 200
    assert client.post(
        f"/contacts/{ids['contact']}", data={"next_step": "sandbox step"}).status_code == 200
    assert client.post(
        f"/contacts/{ids['contact']}/interactions",
        data={"channel": "email", "direction": "outbound", "summary": "hello"},
    ).status_code == 200
    assert client.post(
        "/schedule/log", data={"day": date.today().isoformat(), "key": "outreach"},
    ).status_code == 200
    added = client.post(
        "/jobs/manual", data={"company": "Initech", "title": "Sandbox Engineer"},
        follow_redirects=False)
    assert added.status_code == 303
    assert client.post("/rescore").status_code == 200

    assert len(_sandboxes()) == 1
    conn = sqlite3.connect(_sandboxes()[0])
    assert conn.execute(
        "SELECT lane FROM applications WHERE id = ?", (ids["app"],)).fetchone()[0] == "applied"
    assert conn.execute(
        "SELECT COUNT(*) FROM applications WHERE job_id = ?", (ids["job"],)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT status FROM email_suggestions WHERE id = ?",
        (ids["suggestion"],)).fetchone()[0] == "accepted"
    assert conn.execute(
        "SELECT next_step FROM contacts WHERE id = ?",
        (ids["contact"],)).fetchone()[0] == "sandbox step"
    conn.close()
    tracker = client.get("/tracker").text
    assert "Sandbox Engineer" in tracker and "Initech" in tracker


def test_two_visitors_never_see_each_others_changes(demo_on):
    ids = _ids()
    first, second = _visitor(), _visitor()
    first.post(f"/jobs/{ids['job']}/dismiss")
    first.post("/jobs/manual", data={"company": "Initech", "title": "Only Mine"})
    assert ids["restore"] in first.get("/dismissed").text
    assert "Only Mine" in first.get("/tracker").text
    # The second visitor, and anyone who hasn't changed anything, see the seed.
    for other in (second, _visitor()):
        assert ids["restore"] not in other.get("/dismissed").text
        assert "Only Mine" not in other.get("/tracker").text
    second.post(f"/applications/{ids['app']}/lane", data={"lane": "rejected"})
    assert len(_sandboxes()) == 2
    assert ids["restore"] not in second.get("/dismissed").text
    lanes = set()
    for path in _sandboxes():
        conn = sqlite3.connect(path)
        lanes.add(conn.execute(
            "SELECT lane FROM applications WHERE id = ?", (ids["app"],)).fetchone()[0])
        conn.close()
    assert lanes == {"considering", "rejected"}


def test_one_visitors_cookie_is_no_use_under_another_name(demo_on):
    """A token only works as a whole: a near miss gets the seed, not a sandbox."""
    ids = _ids()
    first = _visitor()
    first.post(f"/jobs/{ids['job']}/dismiss")
    token = _sandboxes()[0].stem
    stranger = _visitor()
    for near in (token[:-1], token.upper(), token + "0", f"./{token}", token.replace("-", "")):
        stranger.cookies.set(demo.COOKIE, near)
        assert ids["restore"] not in stranger.get("/dismissed").text, near


def test_reads_on_a_sandbox_cannot_write(demo_on):
    token = demo.create_sandbox("a")
    conn = demo.connect(token)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE jobs SET dismissed = 1")
    conn.close()
    # Without a sandbox there is nothing to write to: never the seed.
    assert demo._sandbox_connection(None, write=True) is None
    seed = demo.connect()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        seed.execute("UPDATE jobs SET dismissed = 1")
    seed.close()


def test_only_a_write_to_a_sandbox_route_gets_a_writable_connection(demo_on):
    for method, route in (("GET", "/jobs/{job_id}/dismiss"), ("POST", "/sweep"),
                          ("POST", "/profile"), ("POST", None), ("POST", "/nope")):
        conn, made = demo.connection_for(method, route, None, "127.0.0.1")
        assert made is None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE jobs SET dismissed = 1")
        conn.close()
    assert _sandboxes() == []


# --- reset ---------------------------------------------------------------------


def test_reset_throws_the_sandbox_away(client):
    ids = _ids()
    client.post(f"/jobs/{ids['job']}/dismiss")
    assert ids["restore"] in client.get("/dismissed").text
    response = client.post(demo.RESET_PATH, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert f"{demo.COOKIE}=" in _set_cookie(response) and "max-age=0" in _set_cookie(response).lower()
    assert _sandboxes() == []
    assert ids["restore"] not in client.get("/dismissed").text


def test_reset_with_nothing_to_reset_is_harmless(client):
    assert client.post(demo.RESET_PATH, follow_redirects=False).status_code == 303
    client.cookies.set(demo.COOKIE, "../../seed")
    assert client.post(demo.RESET_PATH, follow_redirects=False).status_code == 303
    assert demo.ensure_seed().exists()


def test_a_token_is_looked_up_never_made_into_a_path(demo_on, monkeypatch):
    """Whatever the cookie says, the only files touched are ones found by
    listing the sandbox folder."""
    token = demo.create_sandbox("a")
    assert demo._sandbox_file(token) == demo.sandbox_dir() / f"{token}.db"
    monkeypatch.setattr(demo, "_TOKEN", __import__("re").compile(r"(.*)"))  # even unchecked
    seed = demo.ensure_seed()
    for value in (f"../{seed.stem}", str(seed.with_suffix("")), f"./{token}", f"{token}/..",
                  token.upper(), "..", "\x00"):
        assert demo._sandbox_file(value) is None, value


def test_reset_cannot_delete_outside_the_sandbox_folder(demo_on):
    seed = demo.ensure_seed()
    for value in (seed.stem, f"../{seed.stem}", str(seed.with_suffix("")), "..", ""):
        demo.reset(value)
    assert seed.exists()


def test_every_page_offers_reset(client):
    page = client.get("/tracker").text
    assert f'action="{demo.RESET_PATH}"' in page and "Reset demo" in page


def test_the_everyday_app_has_no_reset_and_sets_no_cookie():
    assert demo.ENABLED is False
    plain = _visitor()
    assert plain.post(demo.RESET_PATH).status_code == 404
    assert "Reset demo" not in plain.get("/profile").text


# --- expiry, the cap and the limits -----------------------------------------------


def test_an_unused_sandbox_disappears_after_an_hour(demo_on):
    token = demo.create_sandbox("a", now=T0)
    path = demo.sandbox_dir() / f"{token}.db"
    assert demo.sandbox(token, now=T0 + demo.IDLE_SECONDS - 5) == path
    # That use counted: the hour starts again from it.
    assert demo.sandbox(token, now=T0 + 2 * demo.IDLE_SECONDS - 10) is not None
    assert demo.sandbox(token, now=T0 + 3 * demo.IDLE_SECONDS) is None
    assert not path.exists()


def test_no_sandbox_outlives_its_day_however_busy(demo_on):
    token = demo.create_sandbox("a", now=T0)
    now = T0
    while now + 1800 <= T0 + demo.MAX_AGE_SECONDS:
        now += 1800
        assert demo.sandbox(token, now=now) is not None
    assert demo.sandbox(token, now=T0 + demo.MAX_AGE_SECONDS + 1) is None
    assert _sandboxes() == []


def test_idle_sandboxes_are_cleared_without_a_new_visitor(demo_on):
    token = demo.create_sandbox("a", now=T0)
    demo.tidy_if_due(now=T0 + 60)
    assert len(_sandboxes()) == 1
    later = T0 + demo.IDLE_SECONDS + demo.TIDY_EVERY_SECONDS + 1
    demo.tidy_if_due(now=later)
    assert _sandboxes() == [] and demo.sandbox(token, now=later) is None


def test_tidying_is_not_repeated_on_every_request(demo_on, monkeypatch):
    calls = []
    monkeypatch.setattr(demo, "_tidy", lambda now, room_for=0: calls.append(now))
    for offset in (0, 1, 2, demo.TIDY_EVERY_SECONDS + 3):
        demo.tidy_if_due(now=T0 + offset)
    assert calls == [T0, T0 + demo.TIDY_EVERY_SECONDS + 3]


def test_past_the_cap_one_that_has_sat_unused_makes_way(demo_on, monkeypatch):
    monkeypatch.setattr(demo, "MAX_SANDBOXES", 3)
    tokens = [demo.create_sandbox(f"client-{n}", now=T0 + n) for n in range(3)]
    later = T0 + demo.EVICT_IDLE_SECONDS + 60
    demo.sandbox(tokens[0], now=later - 30)  # the oldest is used again
    newest = demo.create_sandbox("client-9", now=later)
    assert len(_sandboxes()) == 3
    alive = {t for t in tokens + [newest] if demo.sandbox(t, now=later + 1)}
    assert alive == {tokens[0], tokens[2], newest}  # the least recently used went


def test_a_full_demo_turns_newcomers_away_rather_than_evict_the_active(demo_on, monkeypatch):
    monkeypatch.setattr(demo, "MAX_SANDBOXES", 3)
    tokens = [demo.create_sandbox(f"client-{n}", now=T0 + n) for n in range(3)]
    with pytest.raises(demo.Refused) as refused:
        demo.create_sandbox("client-9", now=T0 + 30)
    assert refused.value.status == 429 and refused.value.reason == demo.BUSY
    assert all(demo.sandbox(t, now=T0 + 31) for t in tokens), "an active visitor lost theirs"
    # Being turned away isn't held against the newcomer.
    assert "client-9" not in demo._started
    job = _ids()["job"]
    response = _visitor().post(f"/jobs/{job}/dismiss", headers={"HX-Request": "true"})
    assert response.status_code == 429 and response.text == demo.BUSY
    assert not _set_cookie(response)
    assert _visitor().get("/tracker").status_code == 200  # looking around still works


def test_one_address_can_only_start_so_many(demo_on, monkeypatch):
    monkeypatch.setattr(demo, "NEW_PER_CLIENT", 2)
    demo.create_sandbox("198.51.100.7", now=T0)
    demo.create_sandbox("198.51.100.7", now=T0 + 1)
    with pytest.raises(demo.Refused) as refused:
        demo.create_sandbox("198.51.100.7", now=T0 + 2)
    assert refused.value.status == 429
    assert len(_sandboxes()) == 2
    demo.create_sandbox("198.51.100.8", now=T0 + 3)  # someone else is unaffected
    demo.create_sandbox("198.51.100.7", now=T0 + demo.NEW_WINDOW_SECONDS + 10)
    assert set(demo._started) == {"198.51.100.7"}, "old counts are forgotten"


def test_a_refused_start_costs_nothing(demo_on, monkeypatch):
    monkeypatch.setattr(demo, "NEW_PER_CLIENT", 1)
    monkeypatch.setattr(demo, "MAX_SANDBOXES", 2)
    first = demo.create_sandbox("a", now=T0)
    other = demo.create_sandbox("b", now=T0 + 1)
    with pytest.raises(demo.Refused):
        demo.create_sandbox("a", now=T0 + demo.EVICT_IDLE_SECONDS + 5)
    # The refusal came before anyone was moved out to make room.
    assert demo.sandbox(first, now=T0 + demo.EVICT_IDLE_SECONDS + 6)
    assert demo.sandbox(other, now=T0 + demo.EVICT_IDLE_SECONDS + 6)
    assert demo._started == {"a": [T0], "b": [T0 + 1]}


def test_the_limit_reaches_the_visitor_as_a_readable_refusal(demo_on, monkeypatch):
    monkeypatch.setattr(demo, "NEW_PER_CLIENT", 1)
    job = _ids()["job"]
    assert _visitor().post(f"/jobs/{job}/dismiss").status_code == 200
    refused = _visitor().post(f"/jobs/{job}/dismiss", headers={"HX-Request": "true"})
    assert refused.status_code == 429 and refused.text == demo.TOO_MANY
    assert not _set_cookie(refused) and len(_sandboxes()) == 1
    page = _visitor().post("/jobs/manual", data={"company": "Initech", "title": "Engineer"})
    assert page.status_code == 429 and demo.TOO_MANY in page.text and "<nav" in page.text


def test_addresses_are_counted_by_household():
    assert demo.client_key("203.0.113.9") == "203.0.113.9"
    assert demo.client_key("2001:db8:1:2:aaaa::1") == demo.client_key("2001:db8:1:2:bbbb::2")
    # One home connection is commonly a /56: all of it is one bucket.
    assert demo.client_key("2001:db8:1:200::1") == demo.client_key("2001:db8:1:2ff::1")
    assert demo.client_key("2001:db8:1:300::1") != demo.client_key("2001:db8:1:200::1")
    assert demo.client_key("::ffff:203.0.113.9") == "203.0.113.9"
    for odd in (None, "", "testclient", "1.2.3.4, 5.6.7.8", "x" * 500):
        assert demo.client_key(odd) == "unknown"


def test_a_sandbox_cannot_grow_without_limit(demo_on, monkeypatch):
    token = demo.create_sandbox("a")
    path = demo.sandbox_dir() / f"{token}.db"
    size = path.stat().st_size
    # SQLite itself stops the file at the cap.
    monkeypatch.setattr(demo, "MAX_SANDBOX_BYTES", size + 2 * demo._HEADROOM_BYTES)
    conn = demo._sandbox_connection(token, write=True)
    with pytest.raises(sqlite3.OperationalError, match="full") as full:
        conn.execute("UPDATE jobs SET description = ?", ("x" * 300_000,))
        conn.commit()
    assert demo.is_full(full.value)
    conn.close()
    assert path.stat().st_size <= size + 2 * demo._HEADROOM_BYTES
    # And once it is nearly there, a write is refused before it starts.
    monkeypatch.setattr(demo, "MAX_SANDBOX_BYTES", size + demo._HEADROOM_BYTES - 1)
    with pytest.raises(demo.Refused) as refused:
        demo._sandbox_connection(token, write=True)
    assert refused.value.reason == demo.FULL
    demo.connect(token).close()  # reading still works


def test_a_full_sandbox_says_so(client, monkeypatch):
    job = _ids()["job"]
    client.post(f"/jobs/{job}/dismiss")
    monkeypatch.setattr(demo, "MAX_SANDBOX_BYTES", demo._HEADROOM_BYTES)
    refused = client.post(f"/jobs/{job}/restore", headers={"HX-Request": "true"})
    assert refused.status_code == 403 and refused.text == demo.FULL
    assert client.get("/dismissed").status_code == 200


def test_sqlite_reporting_full_reads_as_the_same_refusal(client, monkeypatch):
    job = _ids()["job"]

    def full(conn, job_id):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(db, "dismiss_job", full)
    refused = client.post(f"/jobs/{job}/dismiss", headers={"HX-Request": "true"})
    assert refused.status_code == 403 and refused.text == demo.FULL
    assert refused.headers["cache-control"] == "no-store"


def test_a_request_that_fails_outright_leaves_no_orphan_sandbox(demo_on, monkeypatch):
    """Its answer can't carry the cookie, so nobody could ever reach the copy."""
    job = _ids()["job"]

    def broken(conn, job_id):
        raise sqlite3.OperationalError("no such table: jobs")

    monkeypatch.setattr(db, "dismiss_job", broken)
    visitor = _visitor(raise_server_exceptions=False)
    response = visitor.post(f"/jobs/{job}/dismiss")
    assert response.status_code == 500 and response.text == "Internal Server Error"
    assert not _set_cookie(response)
    assert response.headers["cache-control"] == "no-store"
    assert _sandboxes() == []


def test_a_sandbox_dropped_mid_request_does_not_come_back_empty(demo_on, monkeypatch):
    """Opening a file that was deleted a moment ago must not create a new one."""
    ids = _ids()
    visitor = _visitor()
    visitor.post(f"/jobs/{ids['job']}/dismiss")
    path = _sandboxes()[0]
    real = demo.sandbox

    def checked_then_dropped(token, now=None):
        found = real(token, now)
        if found is not None:
            found.unlink()  # another request's reset or tidy, between check and open
        return found

    monkeypatch.setattr(demo, "sandbox", checked_then_dropped)
    page = visitor.get("/dismissed")
    assert page.status_code == 200 and ids["restore"] not in page.text  # the seed
    assert not path.exists()
    # The same thing under a write: no empty file, and a readable refusal.
    visitor.post(f"/jobs/{ids['job']}/dismiss")
    assert _sandboxes() == []
    # With the cookie now naming nothing, the next write starts a fresh copy.
    monkeypatch.setattr(demo, "sandbox", real)
    assert visitor.post(f"/jobs/{ids['job']}/dismiss").status_code == 200
    made = _sandboxes()
    assert len(made) == 1 and made[0] != path and made[0].stat().st_size > 100_000


def test_only_one_rescore_runs_at_a_time(client):
    assert demo._rescore_lock.acquire(blocking=False)
    try:
        busy = client.post("/rescore", headers={"HX-Request": "true"})
    finally:
        demo._rescore_lock.release()
    assert busy.status_code == 429 and busy.text == demo.RESCORE_BUSY
    assert client.post("/rescore").status_code == 200
    assert not demo._rescore_lock.locked()


def test_rescore_runs_off_the_event_loop():
    """A plain def: FastAPI runs it on a worker thread, so a slow Re-score
    can't stop every other request being answered."""
    import inspect

    assert not inspect.iscoroutinefunction(app_module.rescore)


def test_rescore_is_paced_per_sandbox(client):
    assert client.post("/rescore").status_code == 200
    again = client.post("/rescore", headers={"HX-Request": "true"})
    assert again.status_code == 429 and again.text == demo.RESCORE_WAIT
    assert _visitor().post("/rescore").status_code == 200  # another visitor's own clock
    conn = sqlite3.connect(_sandboxes()[0])
    conn.row_factory = sqlite3.Row
    last = float(db.get_state(conn, demo._RESCORED_AT))
    demo.allow_rescore(conn, now=last + demo.RESCORE_GAP_SECONDS + 1)
    with pytest.raises(demo.Refused):
        demo.allow_rescore(conn, now=last + demo.RESCORE_GAP_SECONDS + 2)
    conn.close()


def test_tidying_clears_leftovers_and_keeps_what_is_live(demo_on):
    live = demo.create_sandbox("a", now=T0)
    dead = demo.create_sandbox("b", now=T0 - 2 * demo.IDLE_SECONDS)
    folder = demo.sandbox_dir()
    keep_journal = folder / f"{live}.db-journal"
    dead_journal = folder / f"{dead}.db-journal"
    stale_copy = folder / f"{dead}.copying"
    fresh_copy = folder / "0000000a-in-flight.copying"
    stray = folder / "notes.txt"
    for path in (keep_journal, dead_journal, stale_copy, fresh_copy, stray):
        path.write_bytes(b"x")
    os.utime(stale_copy, (T0 - 3600, T0 - 3600))
    os.utime(fresh_copy, (T0 - 5, T0 - 5))
    kept_folder = folder / "lost+found"  # not ours: left alone, and no error
    kept_folder.mkdir()
    try:
        with demo._sandbox_lock:
            assert demo._tidy(T0 + 1) == 1
        assert sorted(p.name for p in folder.iterdir()) == sorted(
            [f"{live}.db", keep_journal.name, fresh_copy.name, kept_folder.name])
    finally:
        kept_folder.rmdir()


def test_the_sandbox_folder_is_closed_to_other_accounts_even_if_it_existed(demo_on):
    folder = demo.sandbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    demo.create_sandbox("a")
    assert folder.stat().st_mode & 0o077 == 0


# --- what leaves the server --------------------------------------------------------


def test_pages_are_never_cached_but_static_files_are_revalidated(client):
    assert client.get("/tracker").headers["cache-control"] == "no-store"
    changed = client.post(f"/jobs/{_ids()['job']}/dismiss")
    assert changed.headers["cache-control"] == "no-store"
    assert client.get("/static/style.css").headers["cache-control"] == "no-cache"


def test_refused_requests_are_never_cached_and_carry_the_headers(client):
    for method, path in (("POST", "/sweep"), ("POST", "/nope"), ("DELETE", "/tracker")):
        refused = client.request(method, path, headers={"HX-Request": "true"})
        assert refused.status_code == 403
        assert refused.headers["cache-control"] == "no-store"
        assert "Content-Security-Policy" in refused.headers


def test_the_log_names_routes_never_tokens_or_addresses(client, caplog):
    caplog.set_level(logging.INFO, logger="jobsearcher.demo")
    demo.log.propagate = True
    try:
        client.post(f"/jobs/{_ids()['job']}/dismiss")
        client.post("/sweep")
        client.post("/no/such/place%0Aforged line")
    finally:
        demo.log.propagate = False
    text = caplog.text
    assert "sandbox started (1 live)" in text
    assert "refused POST /sweep" in text and "refused POST (no route)" in text
    assert _sandboxes()[0].stem not in text
    assert "forged" not in text and "testclient" not in text


def test_a_prefix_in_front_of_the_app_changes_no_rule(demo_on):
    """Behind a proxy path the policy still follows the route, not the address."""
    prefixed = _visitor(root_path="/demo")
    assert prefixed.post("/demo/sweep").status_code == 403
    assert prefixed.post("/demo/jobs/lookup", data={"url": "https://example.com"}).status_code == 403
    assert prefixed.post(f"/demo/jobs/{_ids()['job']}/dismiss").status_code == 200
    assert len(_sandboxes()) == 1
