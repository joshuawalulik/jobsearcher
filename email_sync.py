"""IMAP inbox → tracker lane suggestions.

Run as: python -m email_sync
Also runs from the tracker's "Sync email" button and after each
autosweep. For
every company with an application in a watched lane, the mailbox is
searched (server-side) for the company name in From/Subject; each hit
is classified against the phrase buckets in profile.yaml's email:
section. Quoted/forwarded history is stripped first, then every bucket
is scored (subject hits weigh extra) and the best bucket wins — a
scheduling email with one stray "unfortunately" no longer reads as a
rejection. Matches become *pending suggestions* on the tracker card —
never automatic lane moves — each carrying the matched phrases as a
human-readable reason, plus the full body and a Gmail deep link so the
original mail can be checked. Deterministic keyword matching only, no
LLM.

Credentials are a Gmail app password in git-ignored config/secrets.yaml
(copy config/secrets.yaml.example); all tunables live in profile.yaml.
"""

import email
import email.header
import email.policy
import email.utils
import imaplib
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import db
from scrapers.common import html_to_text

SECRETS_PATH = Path(__file__).parent / "config" / "secrets.yaml"


def load_secrets(path: Path = SECRETS_PATH) -> dict | None:
    """The gmail: {username, app_password} mapping, or None if not set up."""
    if not path.exists():
        return None
    gmail = (yaml.safe_load(path.read_text()) or {}).get("gmail") or {}
    if gmail.get("username") and gmail.get("app_password"):
        return gmail
    return None


# A line that starts the quoted/forwarded history of a thread. Everything
# from the first such line down is dropped before classifying, so the
# interview email quoted inside a rejection reply can't vote.
_QUOTE_CUT = re.compile(
    r"^(on .{0,200}wrote:"
    r"|-+ ?original message ?-+"
    r"|-+ ?forwarded message ?-+"
    r"|_{10,}"
    r"|from: .+)$",
    re.IGNORECASE,
)


def strip_quoted(body: str) -> str:
    """The new content of a message: cut at the first reply/forward
    marker line and drop '>'-quoted lines."""
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if _QUOTE_CUT.match(stripped):
            break
        if stripped.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines)


def classify(
    subject: str, body: str, buckets: dict[str, list[str]], subject_weight: int = 2
) -> tuple[str | None, str | None]:
    """Score every bucket and pick the best, instead of stopping at the
    first phrase hit anywhere.

    Each phrase found in the (quote-stripped) body counts 1, in the
    subject `subject_weight`. Ties keep the yaml bucket order — rejected
    is listed first so a pure "after your interview, unfortunately…"
    still reads as a rejection. The reason names every matched phrase of
    the winning bucket and any bucket it outscored. Returns
    (lane, reason) or (None, None)."""
    sub = subject.lower()
    bod = strip_quoted(body).lower()
    scored: dict[str, tuple[int, list[str]]] = {}
    for lane, phrases in buckets.items():
        score, matched = 0, []
        for phrase in phrases:
            p = phrase.lower()
            in_subject, in_body = p in sub, p in bod
            if not (in_subject or in_body):
                continue
            score += (subject_weight if in_subject else 0) + (1 if in_body else 0)
            matched.append(f'"{phrase}" (subject)' if in_subject else f'"{phrase}"')
        if score:
            scored[lane] = (score, matched)
    if not scored:
        return None, None
    best = max(scored, key=lambda lane: scored[lane][0])  # stable: yaml order breaks ties
    reason = "matched " + ", ".join(scored[best][1])
    outscored = [lane for lane in scored if lane != best]
    if outscored:
        reason += f" — over {', '.join(outscored)}"
    return best, reason


def reply_marker(text: str, markers: list[str]) -> str | None:
    """The first application-looking phrase in the text, if any — gates
    unclassified mail so company newsletters don't become suggestions."""
    haystack = text.lower()
    for marker in markers:
        if marker.lower() in haystack:
            return marker
    return None


def _decode(value: str | None) -> str:
    """RFC 2047 header (e.g. '=?utf-8?q?...?=') → plain text."""
    if not value:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            chunk = chunk.decode(charset or "utf-8", errors="replace")
        parts.append(chunk)
    return "".join(parts).strip()


def _body_text(msg: email.message.Message) -> str:
    """Plain text of the message: first text/plain part, else de-HTMLed
    text/html, else empty."""
    plain, html_part = None, None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text
    if plain and plain.strip():
        return plain
    return html_to_text(html_part) or ""


def gmail_link(message_id: str | None) -> str | None:
    """Deep link that opens this exact message in the Gmail web UI,
    via its rfc822msgid: search operator."""
    if not message_id:
        return None
    mid = message_id.strip().strip("<>")
    query = urllib.parse.quote(f"rfc822msgid:{mid}", safe="")
    return f"https://mail.google.com/mail/u/0/#search/{query}"


def parse_message(uid: str, raw: bytes) -> dict:
    """Raw RFC822 bytes → {uid, from_name, from_addr, subject,
    received_at, body, message_id}."""
    msg = email.message_from_bytes(raw)
    from_name, from_addr = email.utils.parseaddr(_decode(msg.get("From")))
    received_at = None
    try:
        date = email.utils.parsedate_to_datetime(msg.get("Date"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        received_at = date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass
    return {
        "uid": uid,
        "from_name": from_name,
        "from_addr": from_addr,
        "subject": _decode(msg.get("Subject")),
        "received_at": received_at,
        "body": _body_text(msg),
        "message_id": (msg.get("Message-ID") or "").strip() or None,
    }


def pick_application(apps: list[dict], text: str) -> tuple[dict, str | None]:
    """Which of a company's applications an email belongs to.

    The one whose job title appears in the text wins; otherwise the most
    recently updated, with the ambiguity noted in the returned reason
    suffix (None when unambiguous)."""
    if len(apps) == 1:
        return apps[0], None
    haystack = text.lower()
    for app in apps:
        if app["title"].lower() in haystack:
            return app, None
    return apps[0], f"assumed newest of {len(apps)} applications at this company"


def _connect(cfg: dict, secrets: dict) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(cfg["imap_host"])
    conn.login(secrets["username"], secrets["app_password"])
    return conn


def _search_uids(imap, company: str, since: str) -> list[str]:
    """Server-side search: company name in From or Subject, recent only."""
    status, data = imap.uid(
        "SEARCH", None, "SINCE", since,
        "OR", "FROM", f'"{company}"', "SUBJECT", f'"{company}"',
    )
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].decode().split()


def _fetch_message(imap, uid: str) -> dict | None:
    status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
    if status != "OK":
        return None
    raw = next((item[1] for item in data if isinstance(item, tuple)), None)
    if raw is None:
        return None
    return parse_message(uid, raw)


def sync(conn, profile: dict, *, secrets: dict | None = None, imap=None,
         now: datetime | None = None) -> dict:
    """Scan the inbox and store pending suggestions. Returns summary counts.

    `imap` can be injected for tests; otherwise secrets are required and
    a real IMAP connection is opened (and closed) here.
    """
    cfg = profile["email"]
    now = now or datetime.now(timezone.utc)

    watched = set(cfg["sync_lanes"])
    by_company: dict[str, list[dict]] = {}
    for row in db.fetch_applications(conn):
        if row["lane"] in watched:
            by_company.setdefault(row["company"], []).append(dict(row))

    summary = {"companies": len(by_company), "emails_scanned": 0, "new": 0}
    if not by_company:
        return summary

    own_connection = imap is None
    if own_connection:
        if secrets is None:
            raise ValueError("email sync needs credentials (config/secrets.yaml)")
        imap = _connect(cfg, secrets)
    try:
        imap.select(cfg["folder"], readonly=True)
        since = (now - timedelta(days=cfg["lookback_days"])).strftime("%d-%b-%Y")
        for company, apps in sorted(by_company.items()):
            for uid in _search_uids(imap, company, since):
                msg = _fetch_message(imap, uid)
                if msg is None:
                    continue
                summary["emails_scanned"] += 1
                lane, reason = classify(
                    msg["subject"], msg["body"], cfg["classify"],
                    cfg.get("subject_weight", 2),
                )
                if lane is None:
                    marker = reply_marker(
                        f"{msg['subject']}\n{strip_quoted(msg['body'])}",
                        cfg["reply_markers"],
                    )
                    if marker is None:
                        continue  # company mention but nothing application-like
                    reason = f'reply mentions "{marker}"'
                app, ambiguity = pick_application(
                    apps, f"{msg['subject']}\n{msg['body']}"
                )
                if lane == app["lane"]:
                    continue  # already there — nothing to suggest
                if ambiguity:
                    reason += f" ({ambiguity})"
                if db.add_email_suggestion(
                    conn,
                    application_id=app["id"],
                    msg_uid=msg["uid"],
                    from_addr=msg["from_addr"] or None,
                    subject=msg["subject"] or None,
                    received_at=msg["received_at"],
                    suggested_lane=lane,
                    reason=reason,
                    body=msg["body"] or None,
                    message_id=msg["message_id"],
                ):
                    summary["new"] += 1
    finally:
        if own_connection:
            imap.logout()
    return summary


def format_summary(summary: dict) -> str:
    return (
        f"Checked mail for {summary['companies']} "
        f"compan{'y' if summary['companies'] == 1 else 'ies'}: "
        f"{summary['emails_scanned']} emails scanned, "
        f"{summary['new']} new suggestion{'s' if summary['new'] != 1 else ''}."
    )


def main() -> None:
    from scrapers.sweep import load_profile  # here to keep import-time deps thin

    secrets = load_secrets()
    if secrets is None:
        raise SystemExit(
            "Email sync isn't configured — copy config/secrets.yaml.example "
            "to config/secrets.yaml and fill in your Gmail app password."
        )
    conn = db.get_connection()
    try:
        db.init_db(conn)
        print(format_summary(sync(conn, load_profile(), secrets=secrets)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
