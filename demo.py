"""Demo mode: the app shown publicly on synthetic data.

Switched on by JOBSEARCHER_DEMO=1. In demo mode the app never opens the
real database, profile, conversations or secrets.

Reads come from a synthetic database built by demo_seed for the current
day, through a read-only connection. A visitor's first change copies that
seed into a private sandbox file tied to a cookie, and from then on that
visitor reads and writes their own copy, so nothing one visitor types is
ever shown to another. Anything that would reach the network or change the
shared profile is refused.

The policy lives here so app.py only needs a few hooks. The limits are
constants: mechanics, not preferences.
"""

import ipaddress
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import db
import demo_seed
import security

_FLAG = os.environ.get("JOBSEARCHER_DEMO", "")
if _FLAG not in ("", "0", "1"):
    # "true" or "yes" must not quietly start the full read-write app.
    raise SystemExit(f"JOBSEARCHER_DEMO must be 1 or 0, not {_FLAG!r}")
ENABLED = _FLAG == "1"

PROFILE_PATH = demo_seed.PROFILE_PATH
CONVERSATIONS_DIR = demo_seed.DATA_DIR / "conversations"

log = logging.getLogger("jobsearcher.demo")

# --- what a visitor may do ------------------------------------------------

RESET_PATH = "/demo/reset"

# Routes that change something, each allowed against the visitor's own
# sandbox. Keyed by the route's path as app.py declares it. A write route
# missing from here is refused, and a test fails until it is classified.
SANDBOX_ROUTES = frozenset({
    "/jobs/{job_id}/dismiss",
    "/jobs/{job_id}/restore",
    "/jobs/{job_id}/track",
    "/jobs/manual",
    "/rescore",
    "/applications/{app_id}",
    "/applications/{app_id}/lane",
    "/applications/{app_id}/untrack",
    "/email/suggestions/{sid}/accept",
    "/email/suggestions/{sid}/dismiss",
    "/contacts",
    "/contacts/{contact_id}",
    "/contacts/{contact_id}/interactions",
    "/contacts/{contact_id}/seen",
    "/contacts/{contact_id}/restore",
    "/contacts/{contact_id}/remove",
    "/schedule/log",
    "/schedule/clear",
    "/schedule/excuse",
    "/schedule/unexcuse",
})

# Routes that stay off, each with the reason a visitor is shown.
BLOCKED_ROUTES = {
    "/sweep": "Sweep boards fetches from real job boards, so it is turned off in the demo.",
    "/email/sync": "Sync email reads a real inbox, so it is turned off in the demo.",
    "/jobs/lookup": "Fetching a posting reaches a real job board, so it is turned off in the demo.",
    "/profile": "Every visitor shares one profile, so saving it is turned off in the demo.",
}
NOTICE = "That action is turned off in the demo."

# --- sandbox limits -------------------------------------------------------

COOKIE = "sandbox"
SECURE_COOKIE = "__Host-sandbox"  # over HTTPS: Secure, this host only, Path=/

IDLE_SECONDS = 60 * 60          # a sandbox nobody has used for an hour is gone
MAX_AGE_SECONDS = 12 * 60 * 60  # and none outlives the day its dates came from
MAX_SANDBOXES = 200             # past this, one that has sat unused makes way
EVICT_IDLE_SECONDS = 10 * 60    # but never one somebody used this recently
MAX_SANDBOX_BYTES = 4 * 1024 * 1024  # also bounds what one Re-score can cost
_HEADROOM_BYTES = 1024 * 1024   # stop taking writes this far short of the cap
NEW_PER_CLIENT = 10             # new sandboxes one address may start per window
NEW_WINDOW_SECONDS = 60 * 60
RESCORE_GAP_SECONDS = 10        # Re-score is the one expensive action
TIDY_EVERY_SECONDS = 5 * 60
_STALE_COPY_SECONDS = 60

TOO_MANY = "Too many demo copies were started from this address. Try again in an hour."
BUSY = ("The demo is busy, so it can't keep your changes right now. "
        "You can still look around; try again in a few minutes.")
FULL = "This demo copy is full. Reset the demo to start again."
RESCORE_WAIT = f"Re-score runs at most once every {RESCORE_GAP_SECONDS} seconds in the demo."
RESCORE_BUSY = "Another visitor's Re-score is running. Try again in a moment."

# A token is its creation time and 128 random bits, both in hex. The time
# is trusted only because a token counts for nothing unless a file with
# exactly that name exists, and only this module creates those.
_TOKEN = re.compile(r"([0-9a-f]{8,12})-[0-9a-f]{32}")
_RESCORED_AT = "demo_rescored_at"

_build_lock = threading.Lock()
_sandbox_lock = threading.Lock()
_rescore_lock = threading.Lock()
_started: dict[str, list[float]] = {}  # client -> when each sandbox was started
_last_tidy = 0.0


class Refused(Exception):
    """A request the demo turns away, with the reason the visitor is shown."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def start_logging() -> None:
    """Refusals and new sandboxes go to stderr with a time. Never a request
    body, an address or a token."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s demo: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


def refusal(route_path: str | None) -> str | None:
    """Why a write to this route is refused in demo mode, or None if it may
    run. route_path is the matched route's declared path; None (no route
    matched) is refused like any other unknown."""
    if route_path == RESET_PATH or route_path in SANDBOX_ROUTES:
        return None
    return BLOCKED_ROUTES.get(route_path, NOTICE)


# --- the shared seed ------------------------------------------------------


def data_dir() -> Path:
    """Where seed databases are kept. Git-ignored with the rest of data/."""
    configured = os.environ.get("JOBSEARCHER_DEMO_DIR")
    return Path(configured) if configured else db.DB_PATH.parent / "demo"


def git_sha() -> str | None:
    """The deployed commit, when the deploy sets it."""
    return os.environ.get("JOBSEARCHER_GIT_SHA") or None


def seed_path(now: datetime) -> Path:
    """The seed file for this moment: one per day and sweep slot."""
    return data_dir() / f"seed_{now.date().isoformat()}_{demo_seed.slot_for(now)}.db"


def ensure_seed(now: datetime | None = None) -> Path:
    """Build the current seed if it isn't there, and drop older ones."""
    now = now or datetime.now()
    path = seed_path(now)
    if path.exists():
        return path
    with _build_lock:
        if not path.exists():
            demo_seed.build(path, now.date(), demo_seed.slot_for(now))
            for old in path.parent.glob("seed_*.db"):
                if old != path:
                    old.unlink(missing_ok=True)
    return path


# --- sandboxes ------------------------------------------------------------


def sandbox_dir() -> Path:
    return data_dir() / "sandboxes"


def cookie_name(https: bool) -> str:
    return SECURE_COOKIE if https else COOKIE


def cookie(token: str, https: bool) -> dict:
    """How the sandbox cookie is set: unreadable by scripts, never sent on
    another site's requests, and HTTPS-only wherever the demo is served
    over HTTPS."""
    return {
        "key": cookie_name(https),
        "value": token,
        "max_age": MAX_AGE_SECONDS,
        "path": "/",
        "httponly": True,
        "samesite": "lax",
        "secure": https,
    }


def client_key(host: str | None) -> str:
    """The bucket new sandboxes are counted in: one IPv4 address, or one
    IPv6 /56, the block a home connection is commonly handed."""
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return "unknown"
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 56), strict=False).network_address)
    return str(address)


def _sandbox_file(token: str | None) -> Path | None:
    """This token's sandbox, found among the files that exist, or None.

    What a visitor sends is only ever compared with names already in the
    sandbox folder. It is never made into a path, so no cookie value can
    point the app at a file anywhere else.
    """
    if not token or not _TOKEN.fullmatch(token):
        return None
    wanted = f"{token}.db"
    try:
        with os.scandir(sandbox_dir()) as entries:
            for entry in entries:
                if entry.name == wanted:
                    return Path(entry.path)
    except OSError:  # no sandbox folder yet
        pass
    return None


def _expired(token: str, used: float, now: float) -> bool:
    started = int(_TOKEN.fullmatch(token)[1], 16)
    return now - used > IDLE_SECONDS or now - started > MAX_AGE_SECONDS


def _remove(path: Path) -> None:
    """Delete a sandbox and anything SQLite left beside it."""
    for name in (path.name, f"{path.name}-journal", f"{path.name}-wal", f"{path.name}-shm"):
        path.with_name(name).unlink(missing_ok=True)


def sandbox(token: str | None, now: float | None = None) -> Path | None:
    """The visitor's live sandbox file, or None. Using it keeps it alive."""
    path = _sandbox_file(token)
    if path is None:
        return None
    now = time.time() if now is None else now
    try:
        if _expired(token, path.stat().st_mtime, now):
            _remove(path)
            return None
        os.utime(path, (now, now))
    except OSError:  # no such sandbox, or it was dropped a moment ago
        return None
    return path


def _recent(client: str, now: float) -> list[float]:
    return [at for at in _started.get(client, ()) if now - at < NEW_WINDOW_SECONDS]


def _tidy(now: float, room_for: int = 0) -> int:
    """Delete expired sandboxes and leftovers and forget old counts. Past
    the cap, also the least recently used sandboxes, but only ones nobody
    has used for a while. Returns how many are live. Call with the lock held."""
    for client in list(_started):
        _started[client] = _recent(client, now)
        if not _started[client]:
            del _started[client]
    folder = sandbox_dir()
    if not folder.is_dir():
        return 0
    entries = []
    for path in folder.iterdir():
        try:
            if path.is_file():  # anything else here isn't ours to delete
                entries.append((path, path.stat().st_mtime))
        except OSError:
            continue
    live = {
        path.stem: used for path, used in entries
        if path.suffix == ".db" and _TOKEN.fullmatch(path.stem)
        and not _expired(path.stem, used, now)
    }
    excess = len(live) - (MAX_SANDBOXES - room_for)
    for token in sorted(live, key=live.get)[:max(0, excess)]:
        if now - live[token] >= EVICT_IDLE_SECONDS:
            del live[token]
    for path, used in entries:
        if path.name.split(".", 1)[0] in live:
            continue  # a live sandbox, or the journal of one mid-write
        if path.suffix == ".copying" and now - used < _STALE_COPY_SECONDS:
            continue  # a sandbox being made right now
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
    return len(live)


def tidy_if_due(now: float | None = None) -> None:
    """Clear out expired sandboxes every few minutes, so an idle one is
    deleted even when nobody new arrives. Cheap enough to call per request."""
    global _last_tidy
    now = time.time() if now is None else now
    if now - _last_tidy < TIDY_EVERY_SECONDS:
        return
    if _sandbox_lock.acquire(blocking=False):
        try:
            _last_tidy = now
            _tidy(now)
        finally:
            _sandbox_lock.release()


def _copy_seed(target: Path) -> None:
    for attempt in (1, 2):
        try:
            shutil.copyfile(ensure_seed(), target)
            return
        except FileNotFoundError:  # the seed was replaced at a sweep slot just now
            if attempt == 2:
                raise


def create_sandbox(client: str, now: float | None = None) -> str:
    """Copy the current seed into a new sandbox and return its token.

    Refused when this client has started too many, or when the demo is at
    its cap and every sandbox is in recent use: a newcomer waits rather
    than an active visitor losing their changes.
    """
    now = time.time() if now is None else now
    with _sandbox_lock:
        if len(_recent(client, now)) >= NEW_PER_CLIENT:
            log.info("sandbox refused: too many started from one address")
            raise Refused(429, TOO_MANY)
        live = _tidy(now, room_for=1)
        if live >= MAX_SANDBOXES:
            log.info("sandbox refused: the demo is at its cap")
            raise Refused(429, BUSY)
        _started.setdefault(client, []).append(now)
        token = f"{int(now):08x}-{secrets.token_hex(16)}"
        folder = sandbox_dir()
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)  # the server's own files, not other accounts'
        # Copied under another name first, so a half-written file is never
        # taken for a sandbox.
        scratch = folder / f"{token}.copying"
        _copy_seed(scratch)
        scratch.chmod(0o600)
        os.utime(scratch, (now, now))
        scratch.replace(folder / f"{token}.db")
    log.info("sandbox started (%d live)", live + 1)
    return token


def reset(token: str | None) -> None:
    """Throw away the visitor's sandbox, if they have one."""
    path = _sandbox_file(token)
    if path is not None:
        _remove(path)


def _seed_connection() -> sqlite3.Connection:
    for attempt in (1, 2):
        try:
            return db.get_connection(ensure_seed(), check_same_thread=False, read_only=True)
        except sqlite3.OperationalError:  # replaced at a sweep slot just now
            if attempt == 2:
                raise


def _sandbox_connection(
    token: str | None, write: bool, now: float | None = None
) -> sqlite3.Connection | None:
    """A connection to the visitor's live sandbox, or None if they have none."""
    path = sandbox(token, now)
    if path is None:
        return None
    try:
        if write and path.stat().st_size > MAX_SANDBOX_BYTES - _HEADROOM_BYTES:
            raise Refused(403, FULL)
        # must_exist: a sandbox dropped since the check above must not come
        # back as a new, empty database.
        conn = db.get_connection(path, check_same_thread=False, must_exist=True)
    except (OSError, sqlite3.OperationalError):
        return None
    if write:
        # The hard stop behind the size check: SQLite itself won't grow the
        # file past the cap.
        page_bytes = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count = {MAX_SANDBOX_BYTES // page_bytes}")
    else:
        # query_only rather than a read-only open: it refuses every change
        # just the same, but can still recover a sandbox whose last write
        # was cut off by a restart, which a read-only open cannot.
        conn.execute("PRAGMA query_only = ON")
    return conn


def connect(token: str | None = None, *, now: float | None = None) -> sqlite3.Connection:
    """For reading: the visitor's sandbox if they have a live one, otherwise
    the shared seed. Nothing can be changed through it either way."""
    return _sandbox_connection(token, write=False, now=now) or _seed_connection()


def connection_for(
    method: str, route_path: str | None, token: str | None, client_host: str | None
) -> tuple[sqlite3.Connection, str | None]:
    """The connection one request gets, and the token of a sandbox made for
    it (the caller sets that as the visitor's cookie).

    Only a write to a sandbox route gets a writable connection, and only
    such a write ever creates a sandbox: reading pages, as crawlers do,
    costs nothing. The seed is never opened for writing.
    """
    if method.upper() in security.SAFE_METHODS or route_path not in SANDBOX_ROUTES:
        return connect(token), None
    conn = _sandbox_connection(token, write=True)
    if conn is not None:
        return conn, None
    made = create_sandbox(client_key(client_host))
    conn = _sandbox_connection(made, write=True)
    if conn is None:  # dropped the instant it was made: nothing sane to do
        raise Refused(429, BUSY)
    return conn, made


def allow_rescore(conn: sqlite3.Connection, now: float | None = None) -> None:
    """Refuse a Re-score that follows the visitor's last one too closely."""
    now = time.time() if now is None else now
    last = db.get_state(conn, _RESCORED_AT)
    if last is not None and 0 <= now - float(last) < RESCORE_GAP_SECONDS:
        raise Refused(429, RESCORE_WAIT)
    db.set_state(conn, _RESCORED_AT, repr(now))


@contextmanager
def rescore_turn(conn: sqlite3.Connection):
    """Wrap a Re-score. In the demo only one runs at a time across all
    visitors, and each sandbox is paced; in the everyday app this does
    nothing."""
    if not ENABLED:
        yield
        return
    if not _rescore_lock.acquire(blocking=False):
        raise Refused(429, RESCORE_BUSY)
    try:
        allow_rescore(conn)
        yield
    finally:
        _rescore_lock.release()


def is_full(exc: sqlite3.OperationalError) -> bool:
    """SQLite's own refusal to grow a sandbox past its cap."""
    return "full" in str(exc)
