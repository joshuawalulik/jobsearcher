"""The always-on security controls: one test per promise in security.py.

The route tests run the everyday app (not demo mode) on a throwaway copy of
the synthetic seed, so every page has rows to render and every write route
has something to act on.
"""

import re
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

import app as app_module
import db
import demo_seed
import security
from app import app, get_db

ROOT = Path(__file__).parent.parent
FRONTEND = ROOT / "frontend"
TEMPLATES = sorted(FRONTEND.glob("*.html"))
ANCHOR = date(2026, 3, 11)

PAYLOAD = '"><script>alert(1)</script><img src=x onerror=alert(2)>'
GARBAGE = ("", "x", "-1", "999999999999999999999", "2026-99-99", "nan", "'; DROP TABLE jobs;--",
           "\x00", "☃" * 50, PAYLOAD)
PAGES = ("/", "/?view=all", "/feed", "/tracker", "/schedule", "/schedule/body", "/contacts",
         "/contacts?tab=skipped", "/contacts/body", "/contacts/due-count", "/analytics",
         "/rejected", "/rejected/groups", "/dismissed", "/profile")


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    path = tmp_path_factory.mktemp("security") / "seed.db"
    demo_seed.build(path, ANCHOR, slot=2)
    return path


@pytest.fixture()
def site(seed, tmp_path, monkeypatch):
    """The everyday app on a private copy of the seed, with its own profile."""
    db_path = tmp_path / "jobs.db"
    shutil.copy(seed, db_path)
    profile = tmp_path / "profile.yaml"
    shutil.copy(demo_seed.PROFILE_PATH, profile)
    conversations = tmp_path / "conversations"
    conversations.mkdir()

    def override():
        conn = db.get_connection(db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr(app_module, "PROFILE_PATH", profile)
    monkeypatch.setattr(app_module, "CONVERSATIONS_DIR", conversations)
    client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    client.db_path = db_path
    client.conversations = conversations
    yield client
    app.dependency_overrides.clear()


def _ids(client) -> dict:
    conn = db.get_connection(client.db_path)
    try:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "job": one("SELECT id FROM jobs WHERE gate_failed = 0 AND dismissed = 0 "
                       "AND id NOT IN (SELECT job_id FROM applications) LIMIT 1"),
            "app": one("SELECT id FROM applications WHERE lane = 'considering' LIMIT 1"),
            "contact": one("SELECT id FROM contacts WHERE removed_at IS NULL "
                           "AND status NOT IN ('Skip') AND track = 'agency' LIMIT 1"),
            "suggestion": one("SELECT id FROM email_suggestions WHERE status = 'pending' LIMIT 1"),
        }
    finally:
        conn.close()


def _write_routes() -> list[str]:
    return sorted(
        route.path for route in app.routes
        if (getattr(route, "methods", None) or set()) - security.SAFE_METHODS
    )


# --- which host -----------------------------------------------------------

@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8123",
                                  "LOCALHOST:8000", "[::1]", "[::1]:8000"])
def test_local_names_are_answered(host):
    assert security.host_allowed(host)


@pytest.mark.parametrize("host", [None, "", "evil.example", "evil.example:8000",
                                  "localhost.evil.example", "127.0.0.1.evil.example",
                                  "notlocalhost", "testserver", "localhost:evil.example",
                                  "[::1]evil.example", "127.0.0.1:x", "localhost:8000:1",
                                  "localhost/x", "local host", "user@localhost"])
def test_any_other_name_is_not(host):
    assert not security.host_allowed(host)


def test_a_deployment_adds_its_hostname(monkeypatch):
    monkeypatch.setenv(security.HOSTS_ENV, "demo.example, Other.Example")
    assert security.host_allowed("demo.example")
    assert security.host_allowed("other.example:443")
    assert security.host_allowed("localhost:8000")
    assert not security.host_allowed("evil.example")


def test_a_rebound_hostname_gets_nothing(site):
    response = site.get("/", headers={"Host": "evil.example:8000"})
    assert response.status_code == 400
    assert response.text == security.UNKNOWN_HOST
    assert "Job Search" not in response.text


# --- where the request came from ------------------------------------------

def _cross(method, **headers):
    return security.cross_site(method, Headers({"host": "localhost:8000", **headers}))


@pytest.mark.parametrize("headers", [
    {},                                                    # curl, a script
    {"sec-fetch-site": "same-origin"},                     # the app's own pages
    {"sec-fetch-site": "none"},                            # typed or bookmarked
    {"origin": "http://localhost:8000"},                   # older browser, same origin
])
def test_the_apps_own_writes_pass(headers):
    assert not _cross("POST", **headers)


@pytest.mark.parametrize("headers", [
    {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site"},                       # another port on localhost
    {"sec-fetch-site": "cross-site", "origin": "http://localhost:8000"},  # header wins
    {"origin": "https://evil.example"},
    {"origin": "http://localhost:3000"},
    {"origin": "null"},
])
def test_another_sites_writes_do_not(headers):
    assert _cross("POST", **headers)
    assert _cross("DELETE", **headers)


def test_a_link_into_the_app_works_but_embedding_it_does_not():
    link = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document"}
    assert not _cross("GET", **link)
    assert _cross("GET", **{**link, "sec-fetch-dest": "iframe"})
    assert _cross("GET", **{"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors",
                            "sec-fetch-dest": "image"})
    assert _cross("GET", **{"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"})
    assert not _cross("GET")


def test_every_write_route_refuses_a_cross_site_post(site):
    routes = _write_routes()
    assert len(routes) >= 20
    before = site.db_path.read_bytes()
    for path in routes:
        url = re.sub(r"\{[^}]+\}", "1", path)
        response = site.post(url, headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403, path
        assert response.text == security.CROSS_SITE
    assert site.db_path.read_bytes() == before


def test_a_same_origin_post_still_works(site):
    job = _ids(site)["job"]
    response = site.post(f"/jobs/{job}/track", headers={
        "Sec-Fetch-Site": "same-origin", "Origin": "http://localhost"})
    assert response.status_code == 200


def test_another_site_cannot_read_a_page_in_the_background(site):
    response = site.get("/tracker", headers={
        "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})
    assert response.status_code == 403


# --- response headers ------------------------------------------------------

def test_the_policy_allows_nothing_inline_and_nothing_foreign():
    policy = security.CONTENT_SECURITY_POLICY
    assert "unsafe" not in policy
    assert "*" not in policy and "http" not in policy
    directives = dict(part.split(" ", 1) for part in policy.split("; "))
    assert directives["default-src"] == "'self'"
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["form-action"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"


def test_every_kind_of_response_carries_the_headers(site):
    ids = _ids(site)
    responses = {
        "page": site.get("/"),
        "partial": site.get(f"/jobs/{ids['job']}/detail"),
        "static": site.get("/static/app.js"),
        "not found": site.get("/no/such/page"),
        "bad input": site.get("/schedule?day=nonsense"),
        "refused": site.post("/rescore", headers={"Sec-Fetch-Site": "cross-site"}),
        "unknown host": site.get("/", headers={"Host": "evil.example"}),
        "redirect": site.post("/jobs/manual", data={"company": "Acme", "title": "Engineer"},
                              follow_redirects=False),
        "json": site.get("/health"),
    }
    assert responses["redirect"].status_code == 303
    for kind, response in responses.items():
        for name, value in security.RESPONSE_HEADERS.items():
            assert response.headers.get(name) == value, f"{kind} is missing {name}"


def test_a_conversation_is_looked_up_never_made_into_a_path(site, tmp_path):
    """The address is compared with the files that exist; it never becomes a path."""
    outside = tmp_path / "outside.md"
    outside.write_text("# private")
    (site.conversations / "leads").mkdir()
    (site.conversations / "leads" / "pat.md").write_text("# Pat")
    (site.conversations / "notes.txt").write_text("not a conversation")
    (site.conversations / "link.md").symlink_to(outside)  # a link out of the folder
    assert site.get("/conversations/leads/pat.md").status_code == 200
    for name in ("outside.md", "../outside.md", "leads/../../outside.md", "link.md",
                 "notes.txt", "leads", "leads/", "leads/pat.md/", "LEADS/pat.md",
                 "leads//pat.md", "leads/%2e/pat.md", str(outside), str(outside).lstrip("/")):
        response = site.get(f"/conversations/{name}")
        assert response.status_code == 404, name
        assert "private" not in response.text


def test_refusals_and_errors_are_never_cached(site):
    """A cache in front of the app must not serve one visitor's refusal to the next."""
    refused = [
        site.get("/", headers={"Host": "evil.example"}),
        site.post("/rescore", headers={"Sec-Fetch-Site": "cross-site"}),
        site.get("/?" + "q=" + "x" * (security.MAX_QUERY_BYTES + 1)),
        site.post("/rescore", content=b"x" * (security.MAX_BODY_BYTES + 1)),
    ]
    assert [r.status_code for r in refused] == [400, 403, 414, 413]
    for response in refused:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["Content-Security-Policy"] == security.CONTENT_SECURITY_POLICY


def test_demo_refusals_carry_the_headers_too(site, monkeypatch, tmp_path):
    import demo

    monkeypatch.setenv("JOBSEARCHER_DEMO_DIR", str(tmp_path / "demo"))
    monkeypatch.setattr(demo, "ENABLED", True)
    response = site.post("/sweep")
    assert response.status_code == 403 and demo.BLOCKED_ROUTES["/sweep"] in response.text
    assert response.headers["Content-Security-Policy"] == security.CONTENT_SECURITY_POLICY


def test_there_are_no_generated_api_pages(site):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert site.get(path).status_code == 404, path


# --- nothing inline, so the policy can be strict ---------------------------

_INLINE = {
    "an inline script": re.compile(r"<script(?![^>]*\bsrc=)", re.I),
    "an inline event handler": re.compile(r"<[^>]*\son[a-z]+\s*=", re.I),
    "an hx-on handler": re.compile(r"hx-on[:\w-]*\s*=", re.I),
    "an inline style": re.compile(r"<[^>]*\sstyle\s*=|<style", re.I),
    "a javascript: link": re.compile(r"javascript:", re.I),
    "an htmx filter or js: value, which needs eval": re.compile(
        r"hx-trigger=\"[^\"]*\[|hx-(?:vals|headers|vars)=[\"']\s*(?:js|javascript):", re.I),
}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_templates_hold_no_inline_code_or_styles(template):
    text = template.read_text()
    for what, pattern in _INLINE.items():
        assert not pattern.search(text), f"{template.name} has {what}"


def test_rendered_pages_hold_none_either(site):
    ids = _ids(site)
    panels = (f"/jobs/{ids['job']}/detail", f"/applications/{ids['app']}/modal",
              f"/contacts/{ids['contact']}/modal")
    for path in PAGES + panels:
        response = site.get(path)
        assert response.status_code == 200, path
        for what, pattern in _INLINE.items():
            assert not pattern.search(response.text), f"{path} has {what}"


def test_htmx_is_told_not_to_eval_or_inject():
    base = (FRONTEND / "base.html").read_text()
    config = re.search(r"<meta name=\"htmx-config\" content='([^']+)'", base)[1]
    import json

    settings = json.loads(config)
    assert settings == {"allowEval": False, "allowScriptTags": False,
                        "includeIndicatorStyles": False, "selfRequestsOnly": True}


def test_the_page_script_needs_no_eval():
    script = (FRONTEND / "static" / "app.js").read_text()
    for banned in ("eval(", "new Function", "setTimeout(\"", "document.write", "insertAdjacentHTML"):
        assert banned not in script, banned


def test_every_bar_width_has_a_class():
    css = (FRONTEND / "static" / "style.css").read_text()
    for percent in (0, 1, 37, 99, 100):
        assert f".w-{percent} {{ width: {percent}%; }}" in css
    assert app_module.width_class(37.4) == "w-37"
    assert app_module.width_class(None) == "w-0"
    assert app_module.width_class(-5) == "w-0"
    assert app_module.width_class(250) == "w-100"


# --- how much -------------------------------------------------------------

def test_a_declared_oversize_body_is_refused_before_it_is_read(site):
    headers = Headers({"host": "localhost", "content-length": str(security.MAX_BODY_BYTES + 1)})
    assert security.refusal("POST", headers) == (413, security.TOO_BIG)
    for bad in ("abc", "-1", "1.5", "\u00b2"):
        assert security.refusal(
            "POST", Headers(raw=[(b"host", b"localhost"), (b"content-length", bad.encode("latin-1"))])
        )[0] == 400
    assert security.refusal("POST", Headers({"host": "localhost", "content-length": "10"})) is None
    big = {"company": "Acme", "title": "Engineer", "description": "x" * (security.MAX_BODY_BYTES + 1)}
    response = site.post("/jobs/manual", data=big)
    assert response.status_code == 413


def test_a_chunked_oversize_body_is_refused_too(site):
    def chunks():
        yield b"company=Acme&title=Engineer&description="
        for _ in range(security.MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    response = site.post("/jobs/manual", content=chunks(),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert "content-length" not in response.request.headers
    assert response.status_code == 413 and response.text == security.TOO_BIG
    assert response.headers["Content-Security-Policy"] == security.CONTENT_SECURITY_POLICY


def test_a_very_long_address_is_refused(site):
    assert site.get("/", params={"q": "x" * 100}).status_code == 200
    response = site.get("/", params={"q": "x" * (security.MAX_QUERY_BYTES + 1)})
    assert response.status_code == 414


def test_form_fields_have_length_limits():
    ok = security.form_problem
    assert ok("/jobs/manual", [("title", "x" * security.MAX_FIELD_CHARS)]) is None
    assert "title is too long" in ok("/jobs/manual", [("title", "x" * (security.MAX_FIELD_CHARS + 1))])
    assert ok("/jobs/manual", [("description", "x" * 50_000)]) is None
    assert "description is too long" in ok("/jobs/manual", [("description", "x" * 100_001)])
    assert ok("/profile", [("companies", "x" * 20_000)]) is None
    assert "too long" in ok("/contacts", [("companies", "x" * 20_000)])
    assert ok("/x", [("a", "1")] * security.MAX_FORM_FIELDS) is None
    assert ok("/x", [("a", "1")] * (security.MAX_FORM_FIELDS + 1)) == "Too many form fields."
    assert ok("/x", [("upload", object())]) == "File uploads aren't accepted."


def test_a_refusal_never_repeats_what_was_sent():
    message = security.form_problem("/x", [(PAYLOAD, "y" * 5_000)])
    assert message.startswith("A field is too long")
    assert "script" not in message


def test_an_overlong_field_is_refused_on_every_kind_of_route(site):
    ids = _ids(site)
    long = "x" * (security.MAX_FIELD_CHARS + 1)
    posts = {
        "/jobs/manual": {"company": long, "title": "Engineer"},                # Form(...) fields
        f"/applications/{ids['app']}": {"resume_version": long},               # request.form()
        f"/contacts/{ids['contact']}": {"name": long},
        "/schedule/excuse": {"day": "2026-03-11", "reason": long},
        "/rescore": {"q": long},
    }
    for path, data in posts.items():
        response = site.post(path, data=data)
        assert response.status_code == 422, path
        assert "too long" in response.json()["detail"], path


def test_the_limits_do_not_depend_on_how_the_content_type_is_spelled(site):
    contact = _ids(site)["contact"]
    long = "x" * (security.MAX_FIELD_CHARS + 1)
    for kind in ("application/x-www-form-urlencoded", "Application/X-WWW-Form-Urlencoded",
                 "application/x-www-form-urlencoded; charset=UTF-8"):
        response = site.post(f"/contacts/{contact}", content=f"name={long}",
                             headers={"Content-Type": kind})
        assert response.status_code == 422, kind
    # Spellings the form parser itself doesn't read arrive as an empty form.
    for kind in ("APPLICATION/X-WWW-FORM-URLENCODED; charset=UTF-8", "text/plain", ""):
        site.post(f"/contacts/{contact}", content=f"name={long}", headers={"Content-Type": kind})
    conn = db.get_connection(site.db_path)
    longest = conn.execute("SELECT MAX(LENGTH(name)) FROM contacts").fetchone()[0]
    conn.close()
    assert longest <= security.MAX_FIELD_CHARS
    many = "&".join(f"f{n}=1" for n in range(security.MAX_FORM_FIELDS + 1))
    response = site.post(f"/contacts/{contact}", content=many,
                         headers={"Content-Type": "Application/x-www-form-urlencoded"})
    assert response.status_code == 422


def test_the_profile_allowance_follows_the_route_not_the_address(site):
    """A proxy prefix in front of the app must not change which limits apply."""
    prefixed = TestClient(app, base_url="http://localhost", root_path="/demo",
                          raise_server_exceptions=False)
    contact = _ids(site)["contact"]
    long = "x" * (security.MAX_FIELD_CHARS + 1)
    assert prefixed.post(f"/demo/contacts/{contact}", data={"name": long}).status_code == 422


def test_file_uploads_are_refused(site):
    response = site.post("/jobs/manual", data={"company": "Acme", "title": "Engineer"},
                         files={"description": ("jd.txt", b"hello")})
    assert response.status_code == 422
    assert "uploads" in response.json()["detail"]


def test_real_sized_input_still_fits(site):
    ids = _ids(site)
    description = "We build things. " * 1_500          # about 25,000 characters
    assert site.post("/jobs/manual", data={
        "company": "Acme", "title": "Engineer", "description": description,
    }, follow_redirects=False).status_code == 303
    assert site.post(f"/applications/{ids['app']}", data={"notes": "note\n" * 2_000}).status_code == 200


# --- links ----------------------------------------------------------------

@pytest.mark.parametrize("value, cleaned", [
    ("https://example.com/jobs/1", "https://example.com/jobs/1"),
    ("http://example.com", "http://example.com"),
    ("  example.com/jobs/1 ", "https://example.com/jobs/1"),
    ("", None), (None, None),
    ("javascript://%0aalert(1)", None),
    ("JavaScript://x", None),
    ("data://text/html,<script>alert(1)</script>", None),
    ("file:///etc/passwd", None),
    ("ftp://example.com/x", None),
    ("https://", None),
    ("http://[::1", None),                  # these three make urlsplit raise
    ("https://[not-ipv6]/x", None),
    ("https://x.com\uff0fpath", None),
    (5, None), (b"https://example.com", None),
])
def test_only_web_addresses_count_as_links(value, cleaned):
    assert security.clean_url(value) == cleaned


def test_a_script_address_cannot_be_saved_as_a_posting_link(site):
    response = site.post("/jobs/manual", data={
        "company": "Acme", "title": "Engineer", "url": "javascript://%0aalert(1)"})
    assert response.status_code == 422
    schemeless = site.post("/jobs/manual", data={
        "company": "Acme", "title": "Engineer", "url": "javascript:alert(1)"},
        follow_redirects=False)
    assert schemeless.status_code == 303   # stored as a dead https:// address, never a script


def test_a_script_address_already_in_the_database_is_not_rendered_as_a_link(site):
    ids = _ids(site)
    conn = db.get_connection(site.db_path)
    conn.execute("UPDATE jobs SET url = 'javascript://%0aalert(1)'")
    conn.execute("UPDATE contacts SET linkedin_url = 'javascript://%0aalert(1)'")
    conn.commit()
    conn.close()
    pages = ("/", "/tracker", "/dismissed", "/contacts", f"/jobs/{ids['job']}/detail",
             f"/applications/{ids['app']}/modal", f"/contacts/{ids['contact']}/modal")
    for path in pages:
        response = site.get(path)
        assert response.status_code == 200, path
        assert not re.search(r"(?:href|src|action)\s*=\s*[\"']?\s*javascript", response.text, re.I), path


def test_a_malformed_link_in_the_database_breaks_no_page(site):
    ids = _ids(site)
    conn = db.get_connection(site.db_path)
    conn.execute("UPDATE jobs SET url = 'http://[::1'")
    conn.execute("UPDATE contacts SET linkedin_url = 'https://[not-ipv6]/x'")
    conn.commit()
    conn.close()
    for path in ("/", "/tracker", "/dismissed", "/contacts", f"/jobs/{ids['job']}/detail",
                 f"/applications/{ids['app']}/modal", f"/contacts/{ids['contact']}/modal"):
        assert site.get(path).status_code == 200, path
    assert site.post("/jobs/manual", data={
        "company": "Acme", "title": "Engineer", "url": "http://[::1"}).status_code == 422


def test_a_contact_link_must_be_a_web_address(site):
    contact = _ids(site)["contact"]
    response = site.post(f"/contacts/{contact}", data={"linkedin_url": "javascript://%0aalert(1)"})
    assert response.status_code == 200 and "web address" in response.text
    added = site.post("/contacts", data={
        "track": "agency", "name": "Pat Doe", "linkedin_url": "javascript://%0aalert(1)"})
    assert "web address" in added.text
    conn = db.get_connection(site.db_path)
    stored = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE linkedin_url LIKE 'javascript%'").fetchone()[0]
    conn.close()
    assert stored == 0


# --- escaping --------------------------------------------------------------

def test_text_typed_into_any_field_comes_back_escaped(site):
    ids = _ids(site)
    job, app_id, contact = ids["job"], ids["app"], ids["contact"]
    writes = [
        ("/jobs/manual", {"company": PAYLOAD, "title": PAYLOAD, "location": PAYLOAD,
                          "description": PAYLOAD}),
        (f"/applications/{app_id}", {"notes": PAYLOAD, "resume_version": PAYLOAD,
                                     "next_action": PAYLOAD}),
        (f"/contacts/{contact}", {"name": PAYLOAD, "company": PAYLOAD, "kind_detail": PAYLOAD,
                                  "ask": PAYLOAD, "next_step": PAYLOAD, "verify_flag": PAYLOAD,
                                  "location": PAYLOAD, "notes": PAYLOAD}),
        (f"/contacts/{contact}/interactions", {"channel": "email", "direction": "in",
                                               "summary": PAYLOAD}),
        ("/contacts", {"track": "agency", "name": PAYLOAD, "company": PAYLOAD,
                       "next_step": PAYLOAD, "notes": PAYLOAD, "location": PAYLOAD}),
        ("/schedule/log", {"day": ANCHOR.isoformat(), "key": "coding", "note": PAYLOAD}),
        ("/schedule/excuse", {"day": "2026-03-10", "reason": PAYLOAD}),
    ]
    for path, data in writes:
        response = site.post(path, data=data)
        assert response.status_code < 400, (path, response.status_code, response.text[:200])
        assert PAYLOAD not in response.text, path
    (site.conversations / "note.md").write_text(f"# {PAYLOAD}\n**{PAYLOAD}** {PAYLOAD}\n")

    pages = [*PAGES, f"/schedule?day={ANCHOR.isoformat()}", "/schedule?day=2026-03-10",
             f"/applications/{app_id}/modal", f"/contacts/{contact}/modal",
             f"/jobs/{job}/detail", "/conversations/note.md"]
    for query in ("/", "/feed", "/rejected", "/rejected/groups", "/contacts", "/contacts/body"):
        pages.append(f"{query}?q={PAYLOAD}&location={PAYLOAD}&status={PAYLOAD}&view={PAYLOAD}")
    seen = 0
    for path in pages:
        response = site.get(path)
        assert response.status_code == 200, path
        assert PAYLOAD not in response.text, f"{path} sent the payload back unescaped"
        assert "<script>alert" not in response.text and "<img src=x" not in response.text, path
        seen += response.text.count("&lt;script&gt;alert(1)")
    assert seen > 20, "the payload should be visible, escaped, across the pages"


# --- malformed input never crashes a route ---------------------------------

_FORM_FIELDS = (
    "company", "title", "url", "location", "description", "lane", "notes", "resume_version",
    "next_action", "next_action_due", "applied_at", "status", "priority", "name", "email",
    "linkedin_url", "kind_detail", "ask", "next_step", "date_contacted", "last_reply",
    "verify_flag", "standing_referral", "channel", "direction", "summary", "on_date", "track",
    "source", "day", "key", "note", "reason", "q", "view",
)
# These reach the network or rewrite the profile; they have their own tests.
_NOT_FUZZED = {"/sweep", "/email/sync", "/profile", "/jobs/lookup"}


def test_garbage_in_any_field_is_refused_cleanly_and_breaks_no_page(site):
    ids = _ids(site)
    fill = {"job_id": ids["job"], "app_id": ids["app"], "contact_id": ids["contact"],
            "sid": ids["suggestion"]}
    for path in _write_routes():
        if path in _NOT_FUZZED:
            continue
        url = re.sub(r"\{(\w+)\}", lambda m: str(fill[m[1]]), path)
        for value in GARBAGE:
            response = site.post(url, data={name: value for name in _FORM_FIELDS})
            assert response.status_code < 500, (path, value, response.text[:300])
    # The garbage posts include real actions (untrack, remove), so open the
    # panels of whatever is left rather than the ones that were acted on.
    conn = db.get_connection(site.db_path)
    left_app = conn.execute("SELECT MAX(id) FROM applications").fetchone()[0]
    left_contact = conn.execute(
        "SELECT MAX(id) FROM contacts WHERE removed_at IS NULL").fetchone()[0]
    conn.close()
    for path in (*PAGES, f"/applications/{left_app}/modal", f"/contacts/{left_contact}/modal"):
        assert site.get(path).status_code == 200, f"{path} broke after the garbage"


@pytest.mark.parametrize("path", ["/feed/more?after_score=x&after_id=y", "/schedule?day=x",
                                  "/jobs/abc/detail", "/applications/0/modal",
                                  "/contacts/999999/modal", "/conversations/..%2f..%2fapp.py"])
def test_garbage_in_an_address_is_a_client_error(site, path):
    assert 400 <= site.get(path).status_code < 500


@pytest.mark.parametrize("path", ["/feed/more?after_score=nan&after_id=1",
                                  "/feed/more?after_score=1e999&after_id=-1",
                                  "/?view=%00&q=%00", "/contacts?show_c=x&tab=x&track=x&priority=x",
                                  "/schedule?day=0001-01-01", "/schedule?day=9999-12-31",
                                  "/schedule?day=2000-01-01", "/schedule?day=2100-12-31"])
def test_odd_but_readable_addresses_do_not_crash(site, path):
    assert site.get(path).status_code < 500


HUGE = 2**63


@pytest.mark.parametrize("path", [f"/jobs/{HUGE}/detail", f"/applications/{HUGE}/modal",
                                  f"/contacts/{HUGE}/modal",
                                  f"/feed/more?after_score=1&after_id={HUGE}",
                                  "/conversations/%00.md",
                                  "/conversations/" + "x" * 300 + ".md"])
def test_an_impossible_id_or_name_is_a_client_error_with_headers(site, path):
    response = site.get(path)
    assert 400 <= response.status_code < 500
    assert response.headers["Content-Security-Policy"] == security.CONTENT_SECURITY_POLICY


def test_an_impossible_id_in_a_write_is_a_client_error_too(site):
    for path in (f"/jobs/{HUGE}/track", f"/jobs/{HUGE}/dismiss", f"/applications/{HUGE}/untrack",
                 f"/contacts/{HUGE}/remove", f"/email/suggestions/{HUGE}/accept"):
        assert 400 <= site.post(path).status_code < 500, path


def test_an_unexpected_failure_still_carries_the_headers(site, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom: secret detail")

    monkeypatch.setattr(db, "fetch_dismissed", broken)
    response = site.get("/dismissed")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"      # nothing about the cause
    for name, value in security.RESPONSE_HEADERS.items():
        assert response.headers.get(name) == value


# --- the one route that reaches the network -------------------------------

def test_the_posting_lookup_cannot_be_set_off_by_a_link(site, monkeypatch):
    from scrapers import lookup

    def no_network(*args, **kwargs):
        raise AssertionError("the lookup reached the network")

    monkeypatch.setattr(lookup, "fetch_posting", no_network)
    url = "https://jobs.ashbyhq.com/acme/00000000-0000-0000-0000-000000000000"
    assert site.get("/jobs/lookup", params={"url": url}).status_code == 405
    refused = site.post("/jobs/lookup", data={"url": url}, headers={"Sec-Fetch-Site": "cross-site"})
    assert refused.status_code == 403
    for route in app.routes:      # and no other read reaches it either
        if getattr(route, "endpoint", None) is app_module.lookup_job:
            assert route.methods == {"POST"}


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/a%2F..%2F..%2Fanything%3Fq%3D1%23/jobs/1",
    "https://boards.greenhouse.io/..%2Fx/jobs/1",
    "https://boards.greenhouse.io/a%3Fb/jobs/1",
    "https://jobs.lever.co/a%2Fb/00000000-0000-0000-0000-000000000000",
    "https://jobs.ashbyhq.com/a%23b/00000000-0000-0000-0000-000000000000",
    "https://jobs.ashbyhq.com/%2E%2E/00000000-0000-0000-0000-000000000000",
])
def test_a_board_name_cannot_reshape_the_address_it_is_fetched_from(url):
    from scrapers import lookup

    assert lookup.parse_job_url(url) is None


@pytest.mark.parametrize("url, slug", [
    ("https://job-boards.greenhouse.io/acme/jobs/123", "acme"),
    ("https://jobs.lever.co/acme-labs/00000000-0000-0000-0000-000000000000", "acme-labs"),
    ("https://jobs.ashbyhq.com/acme.example/00000000-0000-0000-0000-000000000000", "acme.example"),
    ("https://jobs.ashbyhq.com/Acme%20Labs/00000000-0000-0000-0000-000000000000", "Acme Labs"),
])
def test_ordinary_board_names_still_parse(url, slug):
    from scrapers import lookup

    assert lookup.parse_job_url(url)[1] == slug
