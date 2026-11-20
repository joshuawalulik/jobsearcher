"""Demo mode: every page works on the synthetic seed, and nothing can change it.

A visitor's changes go to a private copy (tests/test_demo_sandbox.py); what
reaches the network or the shared profile is refused.

demo.ENABLED is read at request time, so these tests switch it on with
monkeypatch. The import-time wiring (profile path, API pages) is checked in
a subprocess started with JOBSEARCHER_DEMO=1, the way the demo really runs.
"""

import hashlib
import imaplib
import os
import re
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db
import demo
import email_sync
from app import app

ROOT = Path(__file__).parent.parent

PAGES = {
    "/": "Track",
    "/?view=all": "Track",
    "/feed": "Track",
    "/tracker": "Acme Corp",
    "/schedule": "Triage",
    "/schedule/body": "Triage",
    "/contacts": "Priya Raman",
    "/contacts?tab=skipped": "Noor Haddad",
    "/contacts/body": "Priya Raman",
    "/contacts/due-count": "",
    "/analytics": "Advanced rate",
    "/rejected": "location",
    "/rejected/groups": "location",
    "/dismissed": "Restore",
    "/profile": "Acme Corp",
    "/conversations/priya-raman.md": "Priya",
}


@pytest.fixture(scope="module")
def seed_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("demo")


@pytest.fixture()
def client(seed_dir, monkeypatch):
    monkeypatch.setenv("JOBSEARCHER_DEMO_DIR", str(seed_dir))
    monkeypatch.setattr(demo, "ENABLED", True)
    monkeypatch.setattr(demo, "_started", {})
    monkeypatch.setattr(app_module, "PROFILE_PATH", demo.PROFILE_PATH)
    monkeypatch.setattr(app_module, "CONVERSATIONS_DIR", demo.CONVERSATIONS_DIR)
    # get_connection's default path is the real database, so a call without
    # a path (or with that path) is exactly what demo mode must never make.
    real_path, real_get = db.DB_PATH, db.get_connection

    def guarded(db_path=None, **kwargs):
        assert db_path is not None and Path(db_path) != real_path, (
            "demo mode opened the real database")
        return real_get(db_path, **kwargs)

    monkeypatch.setattr(db, "get_connection", guarded)
    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client


def _write_routes() -> list[tuple[str, str, str]]:
    """(method, path with ids filled in, declared path) for every route
    that isn't a plain read."""
    found = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        for method in sorted(methods - {"GET", "HEAD", "OPTIONS"}):
            found.append((method, re.sub(r"\{[^}]+\}", "1", route.path), route.path))
    return found


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_page_is_alive(client):
    for path, marker in PAGES.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text, f"{path} is missing {marker!r}"
    assert int(client.get("/contacts/due-count").text) > 0


def test_pages_say_they_are_a_demo(client):
    assert "demo-note" in client.get("/").text


def test_panels_open(client):
    conn = demo.connect()
    app_id = conn.execute("SELECT id FROM applications LIMIT 1").fetchone()[0]
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE gate_failed = 0 AND dismissed = 0 LIMIT 1").fetchone()[0]
    unscored = conn.execute("SELECT id FROM jobs WHERE score IS NULL LIMIT 1").fetchone()[0]
    contact_id = conn.execute(
        "SELECT id FROM contacts WHERE removed_at IS NULL LIMIT 1").fetchone()[0]
    conn.close()
    assert client.get(f"/applications/{app_id}/modal").status_code == 200
    assert client.get(f"/jobs/{job_id}/detail").status_code == 200
    assert client.get(f"/contacts/{contact_id}/modal").status_code == 200
    assert client.get(f"/jobs/{unscored}/detail").status_code == 404


def test_every_write_route_is_classified():
    """A route added later can't reach the demo without a decision about it."""
    declared = {path for _, _, path in _write_routes()}
    assert len(declared) >= 20, "expected the app's write routes"
    known = demo.SANDBOX_ROUTES | set(demo.BLOCKED_ROUTES) | {demo.RESET_PATH}
    assert declared - known == set(), "classify these in demo.py"
    assert known - declared == set(), "demo.py names routes the app no longer has"
    assert not demo.SANDBOX_ROUTES & set(demo.BLOCKED_ROUTES)
    # Every write is a POST, which is what the reset form and HTMX send.
    assert {method for method, _, _ in _write_routes()} == {"POST"}


def test_no_write_ever_changes_the_seed(client):
    seed = demo.ensure_seed()
    before = _digest(seed)
    for method, path, declared in _write_routes():
        response = client.request(method, path, headers={"HX-Request": "true"})
        if declared in demo.BLOCKED_ROUTES:
            assert response.status_code == 403, f"{method} {path}"
            assert response.text == demo.BLOCKED_ROUTES[declared]
        else:
            assert response.status_code != 403, f"{method} {path}"
            assert response.status_code < 500, f"{method} {path}"
    assert _digest(seed) == before
    assert [p.name for p in seed.parent.glob("seed_*.db")] == [seed.name]


def test_a_write_to_no_route_at_all_is_refused(client):
    for method, path in (("POST", "/"), ("POST", "/nope"), ("DELETE", "/jobs/1/dismiss"),
                         ("PUT", "/static/style.css"), ("PATCH", "/tracker")):
        response = client.request(method, path, headers={"HX-Request": "true"})
        assert response.status_code == 403, f"{method} {path}"
        assert response.text == demo.NOTICE
    assert not list(demo.sandbox_dir().glob("*.db"))


def test_a_plain_form_post_gets_a_readable_page(client):
    response = client.post("/profile", data={"anything": "x"})
    assert response.status_code == 403
    assert demo.BLOCKED_ROUTES["/profile"] in response.text and "<nav" in response.text


def test_the_posting_lookup_is_refused(client):
    assert client.post("/jobs/lookup", data={"url": "https://example.com/x"}).status_code == 403


def test_api_pages_are_hidden(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_health_says_only_what_a_deploy_check_needs(client, monkeypatch):
    assert client.get("/health").json() == {"status": "ok", "demo": True, "sha": None}
    monkeypatch.setenv("JOBSEARCHER_GIT_SHA", "abc123")
    assert client.get("/health").json()["sha"] == "abc123"


def test_the_connection_itself_is_read_only(client):
    conn = demo.connect()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE jobs SET dismissed = 1")
    conn.close()


def test_conversations_stay_inside_the_demo_folder(client):
    assert client.get("/conversations/leads/carmen-villanueva.md").status_code == 200
    assert client.get("/conversations/../jobs.yaml").status_code == 404
    assert client.get("/conversations/%2e%2e/tracker.yaml").status_code == 404


def test_no_network_and_no_secrets(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("demo mode tried to reach outside the app")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", refuse)
    monkeypatch.setattr(email_sync, "load_secrets", refuse)
    for path in PAGES:
        assert client.get(path).status_code == 200, path
    conn = demo.connect()
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE gate_failed = 0 AND dismissed = 0 LIMIT 1").fetchone()[0]
    conn.close()
    for method, path, declared in _write_routes():
        blocked = declared in demo.BLOCKED_ROUTES
        assert (client.request(method, path).status_code == 403) is blocked, path
    assert client.post("/jobs/lookup", data={"url": "https://example.com/x"}).status_code == 403
    # The sandbox routes that do real work, with real input, reach nothing either.
    assert client.post(f"/jobs/{job_id}/track").status_code == 200
    assert client.post("/rescore").status_code == 200
    added = client.post("/jobs/manual", data={
        "company": "Initech", "title": "Engineer", "url": "https://example.com/job"})
    assert added.status_code == 200  # followed the redirect to the tracker


def test_old_seeds_are_cleared_when_a_new_one_is_built(tmp_path, monkeypatch):
    from datetime import datetime

    monkeypatch.setenv("JOBSEARCHER_DEMO_DIR", str(tmp_path))
    first = demo.ensure_seed(datetime(2026, 3, 11, 9, 0))
    second = demo.ensure_seed(datetime(2026, 3, 11, 17, 0))
    assert first != second
    assert sorted(p.name for p in tmp_path.iterdir()) == [second.name]
    assert demo.ensure_seed(datetime(2026, 3, 11, 17, 30)) == second


def test_refusal_policy():
    assert demo.refusal("/jobs/{job_id}/track") is None
    assert demo.refusal(demo.RESET_PATH) is None
    assert demo.refusal("/sweep") == demo.BLOCKED_ROUTES["/sweep"]
    assert demo.refusal("/jobs/lookup") == demo.BLOCKED_ROUTES["/jobs/lookup"]
    # Anything unknown is refused: the filled-in address, a new route, no route.
    assert demo.refusal("/jobs/1/track") == demo.NOTICE
    assert demo.refusal("/something/new") == demo.NOTICE
    assert demo.refusal("/static") == demo.NOTICE
    assert demo.refusal(None) == demo.NOTICE


def test_a_misspelled_flag_refuses_to_start():
    """JOBSEARCHER_DEMO=true must not quietly run the full read-write app."""
    for value in ("true", "yes", "on", "2"):
        result = subprocess.run(
            [sys.executable, "-c", "import demo"], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "JOBSEARCHER_DEMO": value},
        )
        assert result.returncode != 0 and "JOBSEARCHER_DEMO must be 1 or 0" in result.stderr
    for value in ("", "0"):
        result = subprocess.run(
            [sys.executable, "-c", "import demo; assert demo.ENABLED is False"], cwd=ROOT,
            capture_output=True, text=True, env={**os.environ, "JOBSEARCHER_DEMO": value},
        )
        assert result.returncode == 0, result.stderr


def test_demo_is_off_unless_asked_for():
    assert demo.ENABLED is False
    assert app_module.PROFILE_PATH != demo.PROFILE_PATH


def test_started_in_demo_mode_the_app_is_wired_to_demo_files(tmp_path):
    """The real startup path: env var set before import."""
    check = (
        "import app, demo, db\n"
        "assert demo.ENABLED\n"
        "assert app.PROFILE_PATH == demo.PROFILE_PATH\n"
        "assert app.CONVERSATIONS_DIR == demo.CONVERSATIONS_DIR\n"
        "assert app.app.docs_url is None and app.app.openapi_url is None\n"
        "from fastapi.testclient import TestClient\n"
        "with TestClient(app.app, base_url=\"http://localhost\") as client:\n"
        "    assert client.get('/health').json()['demo'] is True\n"
        "    assert client.get('/').status_code == 200\n"
        "    assert client.post('/sweep').status_code == 403\n"
        "    changed = client.post('/rescore')\n"
        "    assert changed.status_code == 200\n"
        "    assert 'sandbox=' in changed.headers['set-cookie']\n"
    )
    env = {**os.environ, "JOBSEARCHER_DEMO": "1", "JOBSEARCHER_DEMO_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", check], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".db"]
