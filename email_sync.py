"""IMAP inbox → tracker suggestions.

Run as: python -m email_sync
        python -m email_sync --dry-run [--days N]   (reads only, writes nothing)
Also runs from the tracker's "Sync email" button and after each
autosweep. For every company with an application in a watched lane, the
mailbox is searched (server-side) for the company name in From/Subject,
then each message goes through two pure steps:

triage() says what the mail is. Ignored senders go first, then the lane
buckets: quoted history is stripped, strong and weak phrases score, and
a weak phrase alone can't decide. Without a lane, ignored phrases
(verification codes, account mail) are filtered, automatic receipts are
filed straight into reply history, mail from a person always surfaces,
and the rest needs a reply marker.

route() says which application it belongs to: never one that didn't
exist yet when the mail arrived, the one whose title the mail names
(across every lane, so mail about a closed application is dropped
instead of landing on its neighbour), else the newest open one with the
doubt written into the reason. One email goes to one application, ever.

Nothing moves a lane automatically: lane mail becomes a *pending
suggestion* carrying the phrases that decided it, the sentence they were
in, and the full body and a Gmail deep link. Filtered mail is listed on
the tracker with its reason. Deterministic matching only, no LLM.

Credentials are a Gmail app password in git-ignored config/secrets.yaml
(copy config/secrets.yaml.example); all tunables live in profile.yaml.
"""

import argparse
import email
import email.header
import email.policy
import email.utils
import imaplib
import re
import sqlite3
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

import db
from scrapers.common import html_to_text

SECRETS_PATH = Path(__file__).parent / "config" / "secrets.yaml"

KIND_LABELS = {
    "rejected": "Rejection",
    "offer": "Offer",
    "interviewing": "Interview",
    "receipt": "Receipt",
    "person": "Person",
    "reply": "Reply",
}

EVIDENCE_LIMIT = 240


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


def _flat(text: str | None) -> str:
    """One line with straight apostrophes, so a phrase wrapped across two
    lines of a plain-text mail or typed with a curly quote still matches."""
    return " ".join((text or "").replace("’", "'").split())


def _pattern(entry: str) -> re.Pattern:
    """A yaml phrase as a whole-word, case-insensitive regex: "coding
    challenge" must not fire on "coding challenges the case for"."""
    return re.compile(rf"(?<!\w)(?:{entry})(?!\w)", re.IGNORECASE)


def _clip(text: str, start: int, end: int) -> str:
    """A window of at most EVIDENCE_LIMIT chars around text[start:end]."""
    if len(text) <= EVIDENCE_LIMIT:
        return text
    left = max(0, min(start, end - EVIDENCE_LIMIT // 2))
    return text[left:left + EVIDENCE_LIMIT].strip() + "…"


_SENTENCE_END = re.compile(r"[.!?](?=\s)")


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence of a flattened text containing text[start:end]."""
    left = 0
    for mark in _SENTENCE_END.finditer(text, 0, start):
        left = mark.end()
    mark = _SENTENCE_END.search(text, end)
    right = mark.end() if mark else len(text)
    line = text[left:right]
    return _clip(line.strip(), start - left, end - left)


_GREETING = re.compile(r"^(hi|hello|hey|dear)\b[^,.!:]{0,40}[,.!:]\s*", re.IGNORECASE)


def _opening(text: str) -> str | None:
    """What a mail opens with, greeting dropped, for a person's reply
    where no phrase picked out a sentence."""
    text = _GREETING.sub("", text)
    if not text:
        return None
    mark = _SENTENCE_END.search(text, min(60, len(text)))
    return _clip(text[:mark.end()] if mark else text, 0, 0)


def score_buckets(
    subject: str,
    text: str,
    buckets: dict[str, dict[str, list[str]]],
    *,
    subject_weight: int = 2,
    strong_weight: int = 2,
) -> dict[str, dict]:
    """Every bucket with at least one hit, in yaml order: {lane: {score,
    matched, evidence}}. `subject` and `text` are flattened, text with
    quoted history already gone.

    A phrase counts its tier's weight (strong_weight or 1) once in the
    body and subject_weight times in the subject. `matched` names what
    the mail actually said, not the pattern, so reasons stay readable."""
    scored = {}
    for lane, tiers in buckets.items():
        score, matched, evidence = 0, [], None
        for weight, tier in ((strong_weight, "strong"), (1, "weak")):
            for entry in tiers.get(tier) or []:
                pattern = _pattern(entry)
                in_subject, in_body = pattern.search(subject), pattern.search(text)
                hit = in_subject or in_body
                if hit is None:
                    continue
                score += weight * ((subject_weight if in_subject else 0) + (1 if in_body else 0))
                label = f'"{hit.group(0)}"'
                if in_subject:
                    label += " (subject)"
                elif tier == "weak":
                    label += " (weak)"
                if label not in matched:
                    matched.append(label)
                if in_body and evidence is None:
                    evidence = _sentence(text, in_body.start(), in_body.end())
        if score:
            scored[lane] = {"score": score, "matched": matched, "evidence": evidence}
    return scored


def classify(
    scored: dict[str, dict], min_score: int = 2
) -> tuple[str | None, str | None, str | None]:
    """The best-scoring bucket that reached min_score, as (lane, reason,
    evidence), or three Nones. Ties keep yaml order: rejected is listed
    first so a pure "after your interview, unfortunately…" still reads as
    a rejection. The reason names the phrases of the winner and any other
    bucket that had hits."""
    decided = {lane: hit for lane, hit in scored.items() if hit["score"] >= min_score}
    if not decided:
        return None, None, None
    best = max(decided, key=lambda lane: decided[lane]["score"])  # stable: yaml order
    reason = "matched " + ", ".join(decided[best]["matched"])
    outscored = [lane for lane in scored if lane != best]
    if outscored:
        reason += f" — over {', '.join(outscored)}"
    return best, reason, decided[best]["evidence"]


def reply_marker(text: str, markers: list[str]) -> str | None:
    """The first application-looking phrase in the text, if any — gates
    unclassified mail so company newsletters don't become suggestions."""
    haystack = text.lower()
    for marker in markers:
        if marker.lower() in haystack:
            return marker
    return None


def is_person(msg: dict, cfg: dict) -> bool:
    """Whether this looks written by someone, not sent by a system: not
    mailing-list mail, and neither the address nor the domain is an
    automated one from profile.yaml."""
    if msg.get("bulk"):
        return False
    local, _, domain = (msg.get("from_addr") or "").lower().partition("@")
    if not local or not domain:
        return False
    if any(domain == d or domain.endswith(f".{d}") for d in cfg.get("automated_domains") or []):
        return False
    return not any(re.search(p, local) for p in cfg.get("automated_senders") or [])


def _decision(kind, reason=None, *, lane=None, evidence=None, auto_file=False) -> dict:
    return {"kind": kind, "lane": lane, "reason": reason, "evidence": evidence,
            "auto_file": auto_file}


def _evidence_for(phrase: str, text: str) -> str | None:
    hit = re.search(re.escape(phrase), text, re.IGNORECASE)
    return _sentence(text, hit.start(), hit.end()) if hit else None


def triage(msg: dict, cfg: dict) -> dict:
    """What this mail is: {kind, lane, reason, evidence, auto_file}. Kind
    is a lane bucket, receipt, person, reply, noise (filtered, keep the
    reason) or None (nothing application-like, drop silently)."""
    sender = (msg.get("from_addr") or "").lower()
    for ignored in cfg.get("ignore_senders") or []:
        if ignored.lower() in sender:
            return _decision("noise", f'ignored sender "{ignored}"')

    subject, text = _flat(msg["subject"]), _flat(strip_quoted(msg["body"]))
    scored = score_buckets(
        subject, text, cfg["classify"],
        subject_weight=cfg.get("subject_weight", 2),
        strong_weight=cfg.get("strong_weight", 2),
    )
    lane, reason, evidence = classify(scored, cfg.get("min_score", 2))
    if lane:
        return _decision(lane, reason, lane=lane, evidence=evidence)

    for entry in cfg.get("ignore_phrases") or []:
        pattern = _pattern(entry)
        hit = pattern.search(subject) or pattern.search(text)
        if hit:
            return _decision("noise", f'ignored: mentions "{hit.group(0)}"')

    person = is_person(msg, cfg)
    receipt = next(
        filter(None, (_pattern(e).search(text) for e in cfg.get("receipt_phrases") or [])),
        None,
    )

    # A receipt is only filed unseen when nothing else in it points
    # anywhere: a stray "hiring manager" keeps it in front of the user.
    stray = ", ".join(label for hit in scored.values() for label in hit["matched"])
    if receipt and not person and not stray:
        return _decision(
            "receipt", f'automatic receipt: "{receipt.group(0)}"', auto_file=True,
            evidence=_sentence(text, receipt.start(), receipt.end()),
        )
    if person:
        return _decision("person", "from a person, not an automated sender",
                         evidence=_opening(text))
    if receipt:
        return _decision(
            "reply", f"looks like a receipt, but also says {stray}",
            evidence=next(hit["evidence"] for hit in scored.values()),
        )

    markers = cfg["reply_markers"]
    marker = reply_marker(subject, markers)
    in_body = marker is None
    marker = marker or reply_marker(text, markers)
    if marker is None:
        return _decision(None)
    if in_body and msg.get("bulk"):
        return _decision("noise", f'ignored: mailing list, "{marker}" only in the body')
    return _decision("reply", f'reply mentions "{marker}"',
                     evidence=_evidence_for(marker, text))


def mask_codes(subject: str) -> str:
    """Some senders put the code in the subject line; keep them out of the
    ignored list, which stores no body for the same reason."""
    return re.sub(r"\d{4,}", "••••", subject)


def kind_label(kind: str | None, lane: str | None = None) -> str:
    """Display label; rows older than the kind column go by their lane."""
    return KIND_LABELS.get(kind or lane or "reply", "Reply")


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


# Inline CSS is text to an HTML parser, so it would land in the body.
_STYLE_BLOCK = re.compile(r"<(style|script)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


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
    return html_to_text(_STYLE_BLOCK.sub(" ", html_part or "")) or ""


def gmail_link(message_id: str | None) -> str | None:
    """Deep link that opens this exact message in the Gmail web UI,
    via its rfc822msgid: search operator."""
    if not message_id:
        return None
    mid = message_id.strip().strip("<>")
    query = urllib.parse.quote(f"rfc822msgid:{mid}", safe="")
    return f"https://mail.google.com/mail/u/0/#search/{query}"


def _is_bulk(msg: email.message.Message) -> bool:
    """Mailing-list headers. Some ATS mail carries them too, so this is a
    supporting signal, never a filter by itself."""
    precedence = (msg.get("Precedence") or "").strip().lower()
    return bool(
        msg.get("List-Id") or msg.get("List-Unsubscribe")
        or precedence in ("bulk", "list", "junk")
    )


def parse_message(uid: str, raw: bytes) -> dict:
    """Raw RFC822 bytes → {uid, from_name, from_addr, subject,
    received_at, body, message_id, bulk, to_addrs}."""
    msg = email.message_from_bytes(raw)
    from_name, from_addr = email.utils.parseaddr(_decode(msg.get("From")))
    received_at = None
    try:
        date_header = email.utils.parsedate_to_datetime(msg.get("Date"))
        if date_header.tzinfo is None:
            date_header = date_header.replace(tzinfo=timezone.utc)
        received_at = date_header.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
        "bulk": _is_bulk(msg),
        "to_addrs": [
            addr.lower() for _, addr in email.utils.getaddresses(
                [_decode(v) for v in (msg.get_all("To") or []) + (msg.get_all("Cc") or [])]
            ) if addr
        ],
    }


def _applied_on(app: dict) -> date:
    return date.fromisoformat((app.get("applied_at") or app["created_at"])[:10])


def pick_application(
    apps: list[dict],
    text: str,
    *,
    watched: set[str],
    received_at: str | None = None,
    grace_days: int = 3,
) -> tuple[dict | None, str | None]:
    """Which of a company's applications (every lane) an email belongs to.

    An application that started more than grace_days after the mail
    arrived can't be the subject (the grace covers jobs tracked a little
    after applying). Of the rest, the longest title the mail names wins,
    so naming a closed application returns that application for the
    caller to drop, not its neighbour. Same title twice (a re-application),
    or no title named: an open application, newest first. Returns (app,
    note); the note says when that was a guess, and app is None when
    nothing fits."""
    if received_at:
        cutoff = date.fromisoformat(received_at[:10]) + timedelta(days=grace_days)
        apps = [app for app in apps if _applied_on(app) <= cutoff]
    haystack = _flat(text).lower()
    named = [app for app in apps if _flat(app["title"]).lower() in haystack]
    if named:
        longest = max(len(_flat(app["title"])) for app in named)
        named = [app for app in named if len(_flat(app["title"])) == longest]
    candidates = sorted(
        named or [app for app in apps if app["lane"] in watched],
        key=lambda app: (_applied_on(app), app["id"]),
        reverse=True,
    )
    if not candidates:
        return None, None
    open_apps = [app for app in candidates if app["lane"] in watched]
    note = None
    if len(open_apps) > 1:
        note = f"can't tell which of {len(open_apps)} open applications, put on the newest"
    return (open_apps or candidates)[0], note


def route(msg: dict, decision: dict, apps: list[dict], cfg: dict) -> tuple[dict | None, str | None]:
    """Where a decided mail goes: (application, note), or (None, why not)."""
    watched = set(cfg["sync_lanes"])
    app, note = pick_application(
        apps, f"{msg['subject']}\n{strip_quoted(msg['body'])}",
        watched=watched, received_at=msg["received_at"],
        grace_days=cfg.get("application_grace_days", 3),
    )
    if app is None:
        return None, "no open application it could belong to"
    if app["lane"] not in watched:
        return None, f'it names "{app["title"]}", which is {app["lane"]}'
    if decision["lane"] == app["lane"]:
        return None, f"already {app['lane']}"
    return app, note


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


def _companies_to_scan(conn, cfg: dict) -> dict[str, list[dict]]:
    """Companies with an open application, each with ALL its applications,
    so a mail naming a closed one can be recognised. Keyed case-blind: a
    board can list a company under two spellings of the same name."""
    by_company: dict[str, list[dict]] = {}
    for row in db.fetch_applications(conn):
        by_company.setdefault(row["company"].lower(), []).append(dict(row))
    watched = set(cfg["sync_lanes"])
    return {
        key: apps for key, apps in by_company.items()
        if any(app["lane"] in watched for app in apps)
    }


def _scan(imap, cfg: dict, by_company: dict, seen: set[str], now: datetime):
    """Yield (company, apps, msg, decision) for each unseen matching mail."""
    imap.select(cfg["folder"], readonly=True)
    since = (now - timedelta(days=cfg["lookback_days"])).strftime("%d-%b-%Y")
    for _, apps in sorted(by_company.items()):
        company = apps[0]["company"]
        for uid in _search_uids(imap, company, since):
            if uid in seen:
                continue
            msg = _fetch_message(imap, uid)
            if msg is not None:
                yield company, apps, msg, triage(msg, cfg)


def sync(conn, profile: dict, *, secrets: dict | None = None, imap=None,
         now: datetime | None = None) -> dict:
    """Scan the inbox and store what it finds. Returns summary counts.

    `imap` can be injected for tests; otherwise secrets are required and
    a real IMAP connection is opened (and closed) here.
    """
    cfg = profile["email"]
    now = now or datetime.now(timezone.utc)

    by_company = _companies_to_scan(conn, cfg)
    summary = {"companies": len(by_company), "emails_scanned": 0, "new": 0,
               "receipts": 0, "ignored": 0}
    if not by_company:
        return summary

    own_connection = imap is None
    if own_connection:
        if secrets is None:
            raise ValueError("email sync needs credentials (config/secrets.yaml)")
        imap = _connect(cfg, secrets)
    try:
        seen = db.known_email_uids(conn)
        for company, apps, msg, decision in _scan(imap, cfg, by_company, seen, now):
            summary["emails_scanned"] += 1
            if decision["kind"] is None:
                continue
            if decision["kind"] == "noise":
                seen.add(msg["uid"])
                if db.add_ignored_email(
                    conn,
                    msg_uid=msg["uid"],
                    company=company,
                    from_name=msg["from_name"] or None,
                    from_addr=msg["from_addr"] or None,
                    subject=mask_codes(msg["subject"]) or None,
                    received_at=msg["received_at"],
                    message_id=msg["message_id"],
                    reason=decision["reason"],
                ):
                    summary["ignored"] += 1
                continue
            app, note = route(msg, decision, apps, cfg)
            if app is None:
                continue
            seen.add(msg["uid"])
            reason = decision["reason"] + (f" ({note})" if note else "")
            if db.add_email_suggestion(
                conn,
                application_id=app["id"],
                msg_uid=msg["uid"],
                from_addr=msg["from_addr"] or None,
                from_name=msg["from_name"] or None,
                subject=msg["subject"] or None,
                received_at=msg["received_at"],
                suggested_lane=decision["lane"],
                reason=reason,
                body=msg["body"] or None,
                message_id=msg["message_id"],
                kind=decision["kind"],
                evidence=decision["evidence"],
                status="accepted" if decision["auto_file"] else "pending",
            ):
                summary["receipts" if decision["auto_file"] else "new"] += 1
    finally:
        if own_connection:
            imap.logout()
    return summary


def dry_run(conn, profile: dict, imap, *, days: int | None = None,
            now: datetime | None = None) -> list[dict]:
    """What sync would do with every matching mail in the lookback window,
    already-recorded mail included, without storing anything. The only
    view of mail that gets dropped, since dropped mail is never stored."""
    cfg = dict(profile["email"])
    if days:
        cfg["lookback_days"] = days
    now = now or datetime.now(timezone.utc)
    rows, seen = [], set()
    for company, apps, msg, decision in _scan(
        imap, cfg, _companies_to_scan(conn, cfg), seen, now
    ):
        kind, reason = decision["kind"], decision["reason"]
        if kind is None:
            outcome, reason = "drop", "nothing application-like"
        elif kind == "noise":
            outcome = "ignore"
            seen.add(msg["uid"])
        else:
            app, note = route(msg, decision, apps, cfg)
            if app is None:
                outcome, reason = "skip", f"{reason}; {note}"
            else:
                seen.add(msg["uid"])
                outcome = f"{'file' if decision['auto_file'] else 'suggest'}: {app['title']}"
                reason += f" ({note})" if note else ""
        rows.append({
            "company": company, "received_at": msg["received_at"],
            "sender": msg["from_addr"],
            "subject": _flat(mask_codes(msg["subject"]) if kind == "noise" else msg["subject"]),
            "label": kind_label(kind, decision["lane"]) if kind not in (None, "noise") else None,
            "outcome": outcome, "reason": reason, "evidence": decision["evidence"],
            "bulk": msg["bulk"],
        })
    return rows


def format_dry_run(rows: list[dict]) -> str:
    lines = []
    for row in sorted(rows, key=lambda r: (r["company"].lower(), r["received_at"] or "")):
        label = f"[{row['label']}] " if row["label"] else ""
        lines.append(
            f"{(row['received_at'] or '?')[:10]}  {row['company']}  {row['sender']}"
            f"{'  (list mail)' if row['bulk'] else ''}\n"
            f"  {row['subject']}\n"
            f"  {label}{row['outcome']} — {row['reason']}"
        )
        if row["evidence"]:
            lines.append(f'  "{row["evidence"]}"')
    lines.append(f"\n{len(rows)} emails, dry run: nothing was stored.")
    return "\n".join(lines)


def format_summary(summary: dict) -> str:
    parts = [
        f"{summary['emails_scanned']} emails scanned",
        f"{summary['new']} new suggestion{'s' if summary['new'] != 1 else ''}",
    ]
    if summary["receipts"]:
        parts.append(f"{summary['receipts']} receipt{'s' if summary['receipts'] != 1 else ''} filed")
    if summary["ignored"]:
        parts.append(f"{summary['ignored']} ignored")
    return (
        f"Checked mail for {summary['companies']} "
        f"compan{'y' if summary['companies'] == 1 else 'ies'}: "
        f"{', '.join(parts)}."
    )


def _read_only_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db.DB_PATH.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> None:
    from scrapers.sweep import load_profile  # here to keep import-time deps thin

    parser = argparse.ArgumentParser(description="Inbox → tracker suggestions.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what sync would do; reads Gmail and the DB only")
    parser.add_argument("--days", type=int,
                        help="lookback window for --dry-run (default: email.lookback_days)")
    args = parser.parse_args(argv)

    secrets = load_secrets()
    if secrets is None:
        raise SystemExit(
            "Email sync isn't configured — copy config/secrets.yaml.example "
            "to config/secrets.yaml and fill in your Gmail app password."
        )
    profile = load_profile()
    if args.dry_run:
        conn = _read_only_connection()
        imap = _connect(profile["email"], secrets)
        try:
            print(format_dry_run(dry_run(conn, profile, imap, days=args.days)))
        finally:
            imap.logout()
            conn.close()
        return
    conn = db.get_connection()
    try:
        db.init_db(conn)
        print(format_summary(sync(conn, profile, secrets=secrets)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
