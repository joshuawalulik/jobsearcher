"""Contacts' mail → the interaction log.

Run as: python -m contact_sync
        python -m contact_sync --dry-run [--days N]   (reads only, writes nothing)
        python -m contact_sync --baseline [--days N]  (marks existing mail as seen)
Also runs after each autosweep.

Three sources, all from one read-only pass over Gmail:

- LinkedIn InMail alerts carry the full message. A sender who isn't a contact
  yet becomes a lead: source linkedin, status Not started, so a recruiter's
  pitch never reads as a reply to your outreach.
- LinkedIn message digests only say that someone wrote. They count for known
  contacts and nobody else, because connections' messages are often personal.
- Direct mail to or from a contact's exact address, both ways. Sent mail is
  read from in:sent only, because drafts also sit in All Mail.

Each message is logged once (contact_mail is keyed on its Message-ID), its text
is appended to the contact's saved conversation, and an inbound one flags the
contact under Due today until you reply or dismiss it. Rules live in outreach.py.
Deterministic matching only, no LLM. Your own LinkedIn replies never reach Gmail,
so they stay manual.
"""

import argparse
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import email_sync
import outreach

_THREAD_URL = re.compile(r"https://www\.linkedin\.com/(?:comm/)?messaging/thread/[^\s?]+\S*")
_PROFILE_URL = re.compile(r"https://www\.linkedin\.com/comm/in/([^\s/?]+)\S*")
_RULE = re.compile(r"^-{10,}\s*$", re.M)
_REPLY_PREFIX = re.compile(r"^message replied:\s*", re.IGNORECASE)
_SUFFIX = re.compile(r"\s+via linkedin$|\s*\(via linkedin\)$", re.IGNORECASE)
SUMMARY_LIMIT = 200
EMAIL_BATCH = 25


def _thread_url(link: str) -> str:
    return link.split("?")[0].replace("/comm/", "/").rstrip("/") + "/"


def linkedin_message(body: str) -> dict:
    """{text, thread, headline, profile} of an InMail alert, in either layout.

    One puts the message after the thread link and before LinkedIn's footer
    rule. The other opens with the sender's headline and profile link, then the
    message, then quick-reply lines holding the thread link.
    """
    found = {"text": None, "thread": None, "headline": None, "profile": None}
    profile = _PROFILE_URL.search(body)
    if profile:
        lines = body[:profile.start()].strip().splitlines()
        found["headline"] = lines[-1].strip() if lines else None
        found["profile"] = f"https://www.linkedin.com/in/{profile.group(1)}/"
    start = profile.end() if profile else 0
    link = _THREAD_URL.search(body, start)
    if not link:
        return found
    found["thread"] = _thread_url(link.group())
    if profile:
        line_start = body.rfind("\n", 0, link.start()) + 1
        text = body[start:line_start]
    else:
        rule = _RULE.search(body, link.end())
        text = body[link.end():rule.start() if rule else len(body)]
    found["text"] = text.strip() or None
    return found


def digest_link(body: str) -> str | None:
    """The thread link in a message digest, which has no message text."""
    link = _THREAD_URL.search(body)
    return _thread_url(link.group()) if link else None


def display_name(from_name: str) -> str:
    return _SUFFIX.sub("", from_name or "").strip()


def _summary(subject: str, text: str) -> str:
    opening = email_sync._opening(email_sync._flat(text)) or ""
    line = f"{subject}: {opening}" if subject and opening else subject or opening
    return line if len(line) <= SUMMARY_LIMIT else line[:SUMMARY_LIMIT].rstrip() + "…"


def _inbound_plan(msg, contact, channel, summary, text, thread=None) -> dict:
    return {
        "action": "log", "contact": contact, "direction": "in", "channel": channel,
        "summary": summary, "text": text, "thread": thread,
    }


def _plan_alert(msg: dict, contacts: list, cfg: dict) -> list[dict]:
    name = display_name(msg["from_name"])
    alert = linkedin_message(msg["body"])
    text, thread = alert["text"], alert["thread"]
    if text is None:
        return [{"action": "ignore", "reason": f"no message text from {name}"}]
    title = email_sync._flat(msg["subject"])
    summary = _summary("" if _REPLY_PREFIX.match(title) else title, text)
    contact = outreach.match_contact(name, contacts)
    if contact is not None and contact["removed_at"]:
        return [{"action": "ignore", "reason": f"{name} was removed"}]
    if contact is not None:
        return [_inbound_plan(msg, contact, "linkedin", summary, text, thread)]
    if outreach.name_is_ambiguous(name, contacts):
        return [{"action": "ignore", "reason": f"{name} matches more than one contact"}]
    reason = outreach.ignored_lead(name, f"{msg['subject']} {text}", cfg)
    if reason:
        return [{"action": "ignore", "reason": f"{name}: {reason}"}]
    fields = outreach.lead_fields(
        name, f"{title}\n{text}", text, cfg,
        headline=alert["headline"], profile=alert["profile"],
    )
    return [{
        "action": "add_lead", "direction": "in", "channel": "linkedin", "name": name,
        "summary": summary, "text": text, "thread": thread, "fields": fields,
    }]


def _plan_digest(msg: dict, contacts: list) -> list[dict]:
    contact = outreach.match_contact(display_name(msg["from_name"]), contacts)
    if contact is None or contact["removed_at"]:
        return []  # likely personal: nothing about it is kept
    return [_inbound_plan(
        msg, contact, "linkedin", "Sent a LinkedIn message (text not in the notification)",
        "[LinkedIn only says a message arrived. Open the thread to read it.]",
        digest_link(msg["body"]),
    )]


def plan_message(msg: dict, source: str, contacts: list, cfg: dict) -> list[dict]:
    """What one message means, with no side effects."""
    if source == "alert":
        return _plan_alert(msg, contacts, cfg)
    if source == "digest":
        return _plan_digest(msg, contacts)
    by_email = {c["email"].lower(): c for c in contacts if c["email"] and not c["removed_at"]}
    text = email_sync.strip_quoted(msg["body"]).strip()
    if source == "in":
        contact = by_email.get((msg["from_addr"] or "").lower())
        if contact is None:
            return []
        subject = email_sync._flat(msg["subject"])
        return [_inbound_plan(msg, contact, "email", _summary(subject, text), text)]
    return [
        {
            "action": "log", "contact": by_email[addr], "direction": "out", "channel": "email",
            "summary": f"Emailed: {_summary(email_sync._flat(msg['subject']), text)}",
            "text": text, "thread": None,
        }
        for addr in dict.fromkeys(msg["to_addrs"]) if addr in by_email
    ]


def _local(received_at: str | None) -> datetime:
    """A message's UTC stamp in local time, so its date is the day you saw it."""
    if not received_at:
        return datetime.now()
    utc = datetime.strptime(received_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return utc.astimezone()


def _slug(contact) -> str:
    name = contact["name"] or contact["company"] or f"contact-{contact['id']}"
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def conversation_path(folder: Path, contact) -> Path:
    """The contact's saved file. New files go in leads/ until a real exchange."""
    slug = _slug(contact)
    for path in (folder / f"{slug}.md", folder / "leads" / f"{slug}.md"):
        if path.exists():
            return path
    return folder / ("leads" if outreach.is_lead(contact) else "") / f"{slug}.md"


def save_message(folder: Path, contact, plan: dict, msg: dict) -> tuple[Path, bool]:
    """Append the message to the contact's conversation. Returns (path, created)."""
    path = conversation_path(folder, contact)
    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    when = _local(msg["received_at"])
    who = "You" if plan["direction"] == "out" else contact["name"] or contact["company"]
    lines = []
    if created:
        title = " · ".join(filter(None, (contact["name"], contact["company"])))
        lines += [f"# {title}", "", "Saved automatically by contact_sync.", ""]
    lines += [f"## {when:%Y-%m-%d} ({plan['channel']})", ""]
    if plan.get("thread"):
        lines += [f"Thread: {plan['thread']}", ""]
    subject = _REPLY_PREFIX.sub("", email_sync._flat(msg["subject"]))
    lines += [f"**{who}, {when:%H:%M}**. Subject: {subject}", "", plan["text"], ""]
    with path.open("a", encoding="utf-8") as out:
        if not created:
            out.write("\n")
        out.write("\n".join(lines))
    return path, created


def _apply(conn, plan: dict, msg: dict, cfg: dict, folder: Path) -> str | None:
    """Write one planned message. Returns a refusal, or None."""
    day = _local(msg["received_at"]).date().isoformat()
    if plan["action"] == "add_lead":
        fields, _ = outreach.plan_new_contact(plan["fields"], db.fetch_contacts(conn), cfg)
        plan["contact"] = db.get_contact(conn, db.insert_contact(conn, **fields))
    contact = plan["contact"]
    try:
        if plan["direction"] == "in":
            fields = outreach.plan_inbound(contact, day, cfg)
        else:
            fields = outreach.apply_interaction(contact, "out", day, cfg)
    except ValueError as exc:
        return str(exc)
    db.add_interaction(conn, contact["id"], day, plan["channel"], plan["direction"], plan["summary"])
    if fields:
        db.update_contact(conn, contact["id"], **fields)
    contact = db.get_contact(conn, contact["id"])
    path, _ = save_message(folder, contact, plan, msg)
    saved = f"data/conversations/{path.relative_to(folder)}"
    if saved not in (contact["notes"] or ""):
        notes, _ = outreach.plan_update(
            contact, {"notes": f"Full text in {saved}"}, day, cfg
        )
        db.update_contact(conn, contact["id"], **notes)
    return None


def _uids(imap, query: str) -> list[str]:
    status, data = imap.uid("SEARCH", None, "X-GM-RAW", f'"{query}"')
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].decode().split()


def _queries(cfg: dict, contacts: list, after: str) -> list[tuple[str, str]]:
    queries = [(f"from:{s} after:{after}", "alert") for s in cfg.get("alert_senders") or []]
    queries += [(f"from:{s} after:{after}", "digest") for s in cfg.get("digest_senders") or []]
    emails = sorted({c["email"].lower() for c in contacts if c["email"]})
    for start in range(0, len(emails), EMAIL_BATCH):
        addresses = " OR ".join(emails[start:start + EMAIL_BATCH])
        queries.append((f"from:({addresses}) after:{after}", "in"))
        queries.append((f"in:sent to:({addresses}) after:{after}", "out"))
    return queries


def fetch_new(imap, conn, cfg: dict, contacts: list, now: datetime, days: int) -> list:
    """(message, source) pairs not handled yet, oldest first."""
    after = (now - timedelta(days=days)).strftime("%Y/%m/%d")
    imap.select(cfg["folder"], readonly=True)
    seen = db.seen_contact_mail(conn)
    found: dict[str, tuple] = {}
    for query, source in _queries(cfg, contacts, after):
        for uid in _uids(imap, query):
            msg = email_sync._fetch_message(imap, uid)
            if msg is None:
                continue
            key = msg["message_id"] or f"uid:{uid}"
            msg["key"] = key
            if key not in seen:
                found.setdefault(key, (msg, source))
    return sorted(found.values(), key=lambda item: item[0]["received_at"] or "")


def sync(conn, profile: dict, *, secrets: dict | None = None, imap=None, now=None,
         days: int | None = None, baseline: bool = False,
         folder: Path = db.CONVERSATIONS_DIR) -> dict:
    """One pass. With baseline, mail is marked seen and nothing else happens."""
    cfg = profile["contact_sync"]
    now = now or datetime.now(timezone.utc)
    own_imap = imap is None
    if own_imap:
        imap = email_sync._connect(profile["email"], secrets)
    summary = {"logged": 0, "leads": 0, "ignored": [], "refused": [], "baseline": 0}
    outreach_cfg = profile["outreach"]
    try:
        contacts = db.fetch_contacts(conn, include_removed=True)
        pending = fetch_new(imap, conn, cfg, contacts, now, days or cfg["lookback_days"])
        for msg, source in pending:
            contacts = db.fetch_contacts(conn, include_removed=True)
            if baseline:
                db.record_contact_mail(conn, msg["key"], None, "baseline")
                summary["baseline"] += 1
                continue
            plans = plan_message(msg, source, contacts, {**outreach_cfg, **cfg})
            for plan in plans:
                if plan["action"] == "ignore":
                    summary["ignored"].append(plan["reason"])
                    continue
                refused = _apply(conn, plan, msg, outreach_cfg, folder)
                if refused:
                    summary["refused"].append(refused)
                elif plan["action"] == "add_lead":
                    summary["leads"] += 1
                else:
                    summary["logged"] += 1
            # Digests from non-contacts plan nothing and leave no name behind.
            contact_id = plans[0]["contact"]["id"] if plans and plans[0].get("contact") else None
            outcome = plans[0]["action"] if plans else "skipped"
            db.record_contact_mail(conn, msg["key"], contact_id, outcome)
    finally:
        if own_imap:
            imap.logout()
    return summary


def dry_run(conn, profile: dict, imap, *, days: int | None = None, now=None) -> list[str]:
    """What sync would do, one line per message. Writes nothing."""
    cfg = profile["contact_sync"]
    now = now or datetime.now(timezone.utc)
    contacts = list(db.fetch_contacts(conn, include_removed=True))
    lines = []
    for msg, source in fetch_new(imap, conn, cfg, contacts, now, days or cfg["lookback_days"]):
        day = (msg["received_at"] or "")[:10]
        plans = plan_message(msg, source, contacts, {**profile["outreach"], **cfg})
        for plan in plans:
            who = plan.get("name") or (plan["contact"]["name"] if plan.get("contact") else "")
            label = plan["action"].upper()
            detail = plan.get("reason") or f"{plan.get('direction')} {who}: {plan.get('summary')}"
            extra = plan.get("fields") or {}
            if extra:
                detail += f" | company={extra.get('company')} location={extra.get('location')}"
            lines.append(f"{day} {label:8} {detail}")
    return lines


def format_summary(summary: dict) -> str:
    if summary["baseline"]:
        return f"Contact mail: {summary['baseline']} existing messages marked seen."
    parts = [f"Contact mail: {summary['logged']} logged, {summary['leads']} new leads"]
    if summary["ignored"]:
        parts.append(f"{len(summary['ignored'])} ignored ({'; '.join(summary['ignored'])})")
    if summary["refused"]:
        parts.append(f"{len(summary['refused'])} refused ({'; '.join(summary['refused'])})")
    return ", ".join(parts) + "."


def main(argv: list[str] | None = None) -> None:
    from scrapers.sweep import load_profile  # here to keep import-time deps thin

    parser = argparse.ArgumentParser(description="Contacts' mail → interaction log.")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen")
    parser.add_argument("--baseline", action="store_true",
                        help="mark existing mail as seen without logging it")
    parser.add_argument("--days", type=int,
                        help="lookback window (default: contact_sync.lookback_days)")
    args = parser.parse_args(argv)

    secrets = email_sync.load_secrets()
    if secrets is None:
        raise SystemExit("Email isn't configured: see config/secrets.yaml.example.")
    profile = load_profile()
    conn = db.get_connection()
    try:
        db.init_db(conn)
        if args.dry_run:
            imap = email_sync._connect(profile["email"], secrets)
            try:
                print("\n".join(dry_run(conn, profile, imap, days=args.days)) or "Nothing new.")
            finally:
                imap.logout()
            return
        print(format_summary(
            sync(conn, profile, secrets=secrets, days=args.days, baseline=args.baseline)
        ))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
