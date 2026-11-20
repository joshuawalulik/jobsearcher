"""Deterministic synthetic database for the public demo.

Run as: python -m demo_seed [out_path]

Builds a complete jobs database for the fictional candidate in
config/profile.demo.yaml out of the fragment banks in demo_data/. Nothing
is copied from a real database: the builder creates a fresh file, never
opens an existing one, and refuses to write to db.DB_PATH.

The result is a pure function of (anchor day, slot). Content is the same
every day; only the dates slide, so the feed always has fresh postings,
Due today has items and the schedule has this week's work. The slot is
which of the day's sweeps has already run, so the "last sweep" stamp is
never in the future.

Scores, gate reasons and mail classifications are not invented here.
Postings go through scoring.rescore and mail through email_sync.triage and
route, the same code the real app runs.
"""

import json
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

import db
import email_sync
from scoring.rescore import rescore_all

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "demo_data"
PROFILE_PATH = ROOT / "config" / "profile.demo.yaml"
SEED = 7
CANDIDATE_FIRST = "Alex"
CANDIDATE_EMAIL = "alex@example.com"

# Local sweep times, the same two the launchd agent uses (app.SWEEP_TIMES).
SWEEPS = ((8, 30), (16, 30))
_SWEEP_MINUTES = 6       # a sweep counts as done this long after it starts
_HISTORY_DAYS = 60       # how far back sweeps and postings go
_UTC = "%Y-%m-%d %H:%M:%S"

# Pay bands by years asked, in thousands; high = low + one of the spreads.
_PAY_LOW = {"junior": (96, 102, 112), "mid": (118, 124, 128, 136, 142),
            "senior": (155, 165, 172), "lead": (184, 196, 208)}
_PAY_SPREAD = (26, 32, 38, 44)
_CODING_NOTES = (
    "Sliding window\nLongest substring without repeats\nMinimum window substring",
    "Binary search on the answer: two problems, both under 25 minutes",
    "Graphs: course schedule, number of islands",
    "Heaps: merge k sorted lists, top k frequent",
    "Dynamic programming: coin change, house robber",
    "Intervals: merge, insert, meeting rooms",
)
_PREP_NOTES = (
    "Consistent hashing and how a cache ring rebalances",
    "Designed a rate limiter, out loud, 40 minutes",
    "Two behavioral stories: a missed deadline and a disagreement",
    "Designed a notification service; weak on delivery guarantees",
    "Read about write-ahead logs and replication lag",
)


def slot_for(now: datetime) -> int:
    """How many of the day's sweeps have finished by local time `now`."""
    minutes = now.hour * 60 + now.minute
    return sum(1 for h, m in SWEEPS if minutes >= h * 60 + m + _SWEEP_MINUTES)


class Clock:
    """Turns "N workdays before the anchor" into dates and UTC stamps."""

    def __init__(self, anchor: date, slot: int):
        self.anchor = anchor
        self.slot = slot
        if slot == 0:
            self.ref = datetime(anchor.year, anchor.month, anchor.day)
        else:
            hour, minute = SWEEPS[slot - 1]
            self.ref = self.at(anchor, hour, minute + _SWEEP_MINUTES)

    @staticmethod
    def at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
        """Local wall-clock time on a day; minutes past 59 roll over."""
        return datetime(day.year, day.month, day.day) + timedelta(
            hours=hour, minutes=minute, seconds=second
        )

    @staticmethod
    def utc(local: datetime) -> str:
        """A naive local time as the UTC string SQLite's datetime('now') writes."""
        return local.astimezone(timezone.utc).strftime(_UTC)

    def workday(self, n: int) -> date | None:
        """The day n workdays back. 0 is the anchor, or None on a weekend."""
        if n == 0:
            return self.anchor if self.anchor.weekday() < 5 else None
        day, left = self.anchor, n
        while left:
            day -= timedelta(days=1)
            if day.weekday() < 5:
                left -= 1
        return day

    def last_sunday(self) -> date:
        """The most recent Sunday strictly before the anchor."""
        return self.anchor - timedelta(days=(self.anchor.weekday() + 1) % 7 or 7)

    def sweeps(self) -> list[datetime]:
        """Every sweep start in the history window that has finished, oldest first."""
        out = []
        for back in range(_HISTORY_DAYS, -1, -1):
            day = self.anchor - timedelta(days=back)
            for hour, minute in SWEEPS:
                start = self.at(day, hour, minute)
                if start + timedelta(minutes=_SWEEP_MINUTES) <= self.ref:
                    out.append(start)
        return out


def _rng(name: str) -> random.Random:
    """A separate stream per section, so one section's draws never shift another's."""
    return random.Random(f"{SEED}:{name}")  # nosec B311: repeatable on purpose, nothing secret


def _load(name: str) -> dict:
    return yaml.safe_load((DATA_DIR / name).read_text())


def _insert(conn: sqlite3.Connection, table: str, **cols) -> int:
    # Table and column names are literals in this module, never input.
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO {table} ({names}) VALUES ({marks})",  # nosec B608: see above
        tuple(cols.values()),
    )
    return cur.lastrowid


# --- postings ---------------------------------------------------------------

def _level(years: int | None) -> str:
    if years is None:
        return "mid"
    return "junior" if years <= 1 else "mid" if years <= 4 else "senior" if years <= 7 else "lead"


def _describe(rng, bank, company, name, title, team, family, stack, years, clearance):
    """One posting's text, in the plain-text shape the scrapers store."""
    fill = {"company": name, "title": title, "team": team}
    looking = []
    if years is not None:
        looking.append(rng.choice(bank["years_lines"]).format(n=years))
    variants = bank["stacks"][stack]
    looking += rng.choice(variants) if variants else []
    if clearance:
        looking.append(rng.choice(bank["clearance_lines"]))
    if not looking:
        looking = ["A record of doing careful work and finishing what you start"]
    low = rng.choice(_PAY_LOW[_level(years)])
    pay = rng.choice(bank["benefits"]).format(low=low, high=low + rng.choice(_PAY_SPREAD))
    parts = [
        company["blurb"],
        "About the role\n" + rng.choice(bank["intro"][family]).format(**fill),
        "What you'll do\n" + "\n".join(
            f"- {line.format(**fill)}" for line in rng.sample(bank["does"][family], 4)
        ),
        "What we're looking for\n" + "\n".join(f"- {line}" for line in looking),
        "Nice to have\n" + "\n".join(f"- {line}" for line in rng.sample(bank["nice"], 2)),
        pay,
        rng.choice(bank["closers"]).format(**fill),
    ]
    return "\n\n".join(parts)


def _stub(company, title) -> str:
    """A title-only posting, the kind that earns the sparse-description haircut."""
    return f"{company['blurb']}\n\nWe're hiring a {title}. Apply to learn more about the role."


def _seed_companies(conn, profile, clock) -> dict[str, dict]:
    """Watchlist rows, keyed by name with their new ids."""
    created = clock.utc(clock.at(clock.anchor - timedelta(days=_HISTORY_DAYS), 9))
    by_name = {}
    for entry in profile["companies"]:
        company_id = _insert(
            conn, "companies", name=entry["name"], ats_type=entry["ats"],
            slug=entry["slug"], active=1, created_at=created,
        )
        by_name[entry["name"]] = {"id": company_id, **entry}
    return by_name


def _add_job(conn, company, *, external_id, title, location, department,
             description, posted_at, first_seen, last_seen) -> int:
    return _insert(
        conn, "jobs", company_id=company["id"], external_id=external_id, title=title,
        location=location, department=department,
        url=f"https://example.com/{company['slug']}/jobs/{external_id}",
        description=description, posted_at=posted_at, first_seen=first_seen,
        last_seen=last_seen, dismissed=0,
    )


def _seed_board_jobs(conn, bank, companies, clock, sweeps) -> None:
    """The bulk of the feed: every company's openings, drawn from the banks."""
    rng = _rng("jobs")
    roles = bank["roles"]
    weights = [role["weight"] for role in roles]
    touch = timedelta(minutes=2)       # when a sweep stamps the rows it reads
    latest = sweeps[-1]
    for name, company in bank["companies"].items():
        row = companies[name]
        titles = set()
        for index in range(company["openings"]):
            role = rng.choices(roles, weights)[0]
            team = rng.choice(company["teams"])
            title = rng.choice(role["titles"]).format(team=team)
            if title in titles:    # a board rarely lists the same title twice
                title = rng.choice(role["titles"]).format(team=rng.choice(company["teams"]))
            titles.add(title)
            years = rng.choice(role["years"])
            stack = rng.choice(role["stacks"])
            location = rng.choice(bank["locations"][rng.choice(company["locations"])])
            clearance = bool(company.get("clearance")) and rng.random() < 0.7
            roll = rng.random()
            if roll < 0.04:
                description = _stub(company, title)
            else:
                description = _describe(rng, bank, company, name, title, team,
                                        role["family"], stack, years, clearance)
            # Mostly recent, with a long tail, and a handful new at the last sweep.
            age = rng.random()
            if age < 0.04:
                seen = latest
            else:
                back = (rng.randint(1, 3) if age < 0.25 else
                        rng.randint(4, 14) if age < 0.65 else rng.randint(15, 45))
                seen = sweeps[max(0, len(sweeps) - 1 - back * len(SWEEPS) - rng.randint(0, 1))]
            last = latest
            if (latest - seen).days >= 6 and rng.random() < 0.18:
                # Taken down a while ago: the delisted gate drops these.
                gone = rng.randint(3, (latest - seen).days)
                last = sweeps[max(0, len(sweeps) - 1 - gone * len(SWEEPS))]
                last = max(last, seen)
            posted = None if rng.random() < 0.06 else clock.utc(
                seen - timedelta(hours=rng.randint(1, 72)))
            _add_job(
                conn, row, external_id=str(4_000_000 + row["id"] * 1000 + index),
                title=title, location=None if rng.random() < 0.02 else location,
                department=role["department"], description=description,
                posted_at=posted, first_seen=clock.utc(seen + touch),
                last_seen=clock.utc(last + touch),
            )


# --- tracker ----------------------------------------------------------------

_STEP_WINDOWS = {   # local (hour, minute) ranges by the lane a step moves to
    "considering": ((8, 40), (9, 25)),
    "ready": ((10, 35), (11, 30)),
    "applied": ((11, 35), (12, 25)),
    "skipped": ((10, 40), (11, 50)),
    "interviewing": ((14, 0), (17, 20)),
    "rejected": ((14, 0), (17, 20)),
    "offer": ((14, 0), (17, 20)),
}


def _step_times(entry, clock, excused) -> list[tuple[datetime, str]]:
    """(local time, lane) for each scripted step that has happened by now."""
    rng = _rng(f"app:{entry['key']}")
    out: list[tuple[datetime, str]] = []
    for position, step in enumerate(entry["steps"]):
        when, lane = step[0], step[1]
        (h1, m1), (h2, m2) = _STEP_WINDOWS[lane]
        minute = rng.randint(h1 * 60 + m1, h2 * 60 + m2)   # drawn even if unused
        if when == "sunday":
            day = clock.last_sunday()
        else:
            day = clock.workday(when)
            if day == excused:
                raise ValueError(f"{entry['key']}: a step falls on the excused day")
        if day is None or (len(step) > 2 and step[2] > clock.slot and day == clock.anchor):
            break
        stamp = clock.at(day, 0, minute)
        if day == clock.anchor:
            if clock.slot == 0:
                break
            # Today's work is stamped just after the sweep that has run.
            stamp = clock.ref + timedelta(minutes=2 + 3 * position)
        if out and stamp <= out[-1][0]:
            stamp = out[-1][0] + timedelta(minutes=rng.randint(4, 30))
        out.append((stamp, lane))
    return out


def _seed_tracker(conn, bank, script, companies, clock, sweeps) -> dict[str, dict]:
    """Scripted applications: a posting each, then the lane history as events."""
    excused = clock.workday(script["excused_workday"])
    latest, first = sweeps[-1], sweeps[0]
    touch = timedelta(minutes=2)
    apps: dict[str, dict] = {}
    entries = [*script["applications"], {**script["manual"], "manual": True}]
    for index, entry in enumerate(entries):
        rng = _rng(f"posting:{entry['key']}")
        name = entry["company"]
        if entry.get("manual"):
            created = clock.utc(clock.at(clock.anchor - timedelta(days=20), 9))
            company = {"blurb": f"{name} is a design and engineering consultancy.",
                       "teams": ["Client Engineering"]}
            row = {"slug": "hartwell-reed", "id": _insert(
                conn, "companies", name=name, ats_type="manual", slug="hartwell-reed",
                active=1, created_at=created)}
        else:
            company, row = bank["companies"][name], companies[name]
        title = entry["title"]
        team = title.split(", ", 1)[1] if ", " in title else company["teams"][0]
        description = _describe(rng, bank, company, name, title, team, "product",
                                entry["stack"], entry["years"], entry.get("clearance", False))
        location = rng.choice(bank["locations"][entry["location"]])

        steps = _step_times(entry, clock, excused)
        first_step = entry["steps"][0][0]
        tracked_day = steps[0][0].date() if steps else (
            clock.workday(first_step) or clock.anchor)
        seen = clock.at(tracked_day - timedelta(days=entry.get("seen_before", 0)), 8, 30)
        seen = max(first, min(seen, latest))
        last = latest
        if steps and steps[-1][1] == "rejected" and (clock.anchor - steps[-1][0].date()).days > 14:
            # The role was filled, so the posting came down soon after.
            last = clock.at(steps[-1][0].date() + timedelta(days=2), 8, 30)
        job_id = _add_job(
            conn, row, external_id=(f"manual-demo-{index}" if entry.get("manual")
                                    else str(5_000_000 + index)),
            title=title, location=location, department="Engineering",
            description=description,
            posted_at=clock.utc(seen - timedelta(hours=rng.randint(2, 40))),
            first_seen=clock.utc(seen + touch), last_seen=clock.utc(last + touch),
        )
        if not steps:
            continue    # not tracked yet at this time of day; just a posting
        lane = steps[-1][1]
        applied = next((at for at, to in steps if to == "applied"), None)
        reached = {to for _, to in steps}
        app_id = _insert(
            conn, "applications", job_id=job_id, lane=lane,
            resume_version=entry.get("resume") if reached & {"ready", "applied"} else None,
            notes=entry.get("notes"), next_action=entry.get("next_action"),
            next_action_due=(clock.anchor + timedelta(days=entry["due"])).isoformat()
            if "due" in entry else None,
            applied_at=applied.date().isoformat() if applied else None,
            created_at=clock.utc(steps[0][0]), updated_at=clock.utc(steps[-1][0]),
        )
        _insert(conn, "events", application_id=app_id, type="tracked",
                detail="considering", occurred_at=clock.utc(steps[0][0]))
        for (_, before), (at, to) in zip(steps, steps[1:]):
            _insert(conn, "events", application_id=app_id, type="lane_change",
                    detail=f"{before} -> {to}", occurred_at=clock.utc(at))
        apps[entry["key"]] = {"id": app_id, "job_id": job_id, "company": name,
                              "title": title, "steps": steps}
    return apps


def _as_of(app: dict, moment: datetime) -> dict | None:
    """The application as email_sync would have read it at `moment`."""
    done = [(at, lane) for at, lane in app["steps"] if at <= moment]
    if not done:
        return None
    applied = next((at for at, lane in done if lane == "applied"), None)
    return {
        "id": app["id"], "title": app["title"], "company": app["company"],
        "lane": done[-1][1],
        "applied_at": applied.date().isoformat() if applied else None,
        "created_at": Clock.utc(done[0][0]),
    }


def _mail_time(message, apps, clock) -> datetime | None:
    when = message["when"]
    if "workdays" in when:
        day = clock.workday(when["workdays"])
        hour, minute = (int(part) for part in when["at"].split(":"))
        return clock.at(day, hour, minute) if day else None
    lane = when.get("after") or when["before"]
    app = apps.get(message["app"])
    step = next((at for at, to in (app or {"steps": []})["steps"] if to == lane), None)
    if step is None:
        return None
    return step + timedelta(minutes=when["minutes"] * (1 if "after" in when else -1))


def _seed_mail(conn, corpus, apps, profile, clock) -> list[dict]:
    """Run each message through the real triage and routing, and store the result.

    Returns one row per message with what the classifier said, so tests (and
    later the evals) can compare it with the human label.
    """
    cfg = profile["email"]
    watched = set(cfg["sync_lanes"])
    timed = []
    for message in corpus["messages"]:
        at = _mail_time(message, apps, clock)
        if at is not None and at <= clock.ref:
            timed.append((at, message))
    timed.sort(key=lambda pair: (pair[0], pair[1]["id"]))

    results = []
    for at, message in timed:
        subject_app = apps.get(message.get("app") or "")
        company = subject_app["company"] if subject_app else message["company"]
        fill = {"first": CANDIDATE_FIRST, "company": company,
                "title": subject_app["title"] if subject_app else ""}
        msg = {
            "uid": message["id"],
            "from_name": message["from_name"],
            "from_addr": message["from_addr"],
            "subject": message["subject"].format(**fill),
            "body": message["body"].format(**fill),
            "received_at": clock.utc(at),
            "message_id": None,     # no "Open in Gmail" link on synthetic mail
            "bulk": bool(message.get("bulk")),
            "to_addrs": [CANDIDATE_EMAIL],
        }
        at_company = [
            (key, state) for key, app in apps.items()
            if app["company"] == company and (state := _as_of(app, at))
        ]
        result = {"id": message["id"], "expect": message["expect"],
                  "known_miss": bool(message.get("known_miss")),
                  "kind": "drop", "app": None}
        results.append(result)
        if not any(state["lane"] in watched for _, state in at_company):
            continue    # no open application: a sync never searches this company
        decision = email_sync.triage(msg, cfg)
        if decision["kind"] is None:
            continue
        stored = clock.utc(at + timedelta(minutes=1))
        if decision["kind"] == "noise":
            result["kind"] = "noise"
            _insert(
                conn, "email_ignored", msg_uid=msg["uid"], company=company,
                from_name=msg["from_name"], from_addr=msg["from_addr"],
                subject=email_sync.mask_codes(msg["subject"]),
                received_at=msg["received_at"], message_id=None,
                reason=decision["reason"], created_at=stored,
            )
            continue
        target, note = email_sync.route(msg, decision, [s for _, s in at_company], cfg)
        if target is None:
            continue
        result["kind"] = decision["kind"]
        result["app"] = next(key for key, state in at_company if state["id"] == target["id"])
        _insert(
            conn, "email_suggestions", application_id=target["id"], msg_uid=msg["uid"],
            from_addr=msg["from_addr"], subject=msg["subject"],
            received_at=msg["received_at"], body=msg["body"], message_id=None,
            from_name=msg["from_name"], kind=decision["kind"],
            evidence=decision["evidence"], suggested_lane=decision["lane"],
            reason=decision["reason"] + (f" ({note})" if note else ""),
            status="accepted" if decision["auto_file"] else message.get("seed", "pending"),
            created_at=stored,
        )
    return results


# --- everything else --------------------------------------------------------

def _seed_dismissals(conn, clock, excused) -> None:
    """A few jobs waved off each recent morning, low scores first."""
    rng = _rng("dismissals")
    candidates = [row["id"] for row in conn.execute(
        """SELECT j.id FROM jobs j
           WHERE j.gate_failed = 0 AND j.score < 40
             AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)
           ORDER BY j.id"""
    )]
    rng.shuffle(candidates)
    for back in range(0, 11):
        day = clock.workday(back)
        count = rng.randint(1, 4)       # drawn every day so later days don't shift
        if day is None or day == excused or (back == 0 and clock.slot == 0):
            continue
        for offset in range(count):
            if not candidates:
                return
            at = clock.at(day, 8, 41 + offset * 3)
            if day == clock.anchor:
                at = clock.ref + timedelta(minutes=1, seconds=20 * offset)
            conn.execute(
                "UPDATE jobs SET dismissed = 1, dismissed_at = ? WHERE id = ?",
                (clock.utc(at), candidates.pop()),
            )


def _seed_contacts(conn, people, apps, clock) -> None:
    def day_of(entry, key):
        day = clock.workday(entry[key]) if key in entry else None
        return day.isoformat() if day else None

    for entry in people["contacts"]:
        notes = entry.get("notes")
        if "conversation" in entry:
            pointer = f"Conversation: data/conversations/{entry['conversation']}"
            notes = f"{notes}\n{pointer}" if notes else pointer
        oldest = max([entry.get("contacted", 0), *(i[0] for i in entry.get("interactions", []))])
        created = clock.utc(clock.at(clock.workday(oldest + 2), 9, 10))
        removed = clock.workday(entry["removed"]) if "removed" in entry else None
        contact_id = _insert(
            conn, "contacts", name=entry.get("name"), company=entry.get("company"),
            email=entry.get("email"), track=entry["track"],
            kind_detail=entry.get("kind_detail"), priority=entry.get("priority"),
            ask=entry.get("ask"), status=entry["status"],
            date_contacted=day_of(entry, "contacted"), last_reply=day_of(entry, "replied"),
            next_step=entry.get("next_step"), notes=notes, source=entry["source"],
            peer_history=entry.get("peer_history"), verify_flag=entry.get("verify_flag"),
            standing_referral=int(bool(entry.get("standing_referral"))),
            location=entry.get("location"), new_message=day_of(entry, "new_message"),
            removed_at=clock.utc(clock.at(removed, 9, 30)) if removed else None,
            created_at=created, updated_at=created,
        )
        for order, item in enumerate(entry.get("interactions", [])):
            back, channel, direction, summary = item[:4]
            day = clock.workday(back)
            if day is None or (back == 0 and clock.slot < (item[4] if len(item) > 4 else 1)):
                continue
            _insert(
                conn, "interactions", contact_id=contact_id, date=day.isoformat(),
                channel=channel, direction=direction, summary=summary,
                created_at=clock.utc(min(clock.at(day, 9, 35 + order), clock.ref)),
            )
        linked = apps.get(entry.get("link") or "")
        if linked:
            _insert(
                conn, "contact_applications", contact_id=contact_id,
                application_id=linked["id"], note=None,
                created_at=clock.utc(linked["steps"][0][0]),
            )


def _seed_sweep_runs(conn, clock, sweeps, company_count) -> None:
    """Two weeks of run history; the last row matches the database as built."""
    recent = sweeps[-28:]
    flaky = {len(recent) - 6: ["Kestrel Systems: read timed out after 10s"],
             len(recent) - 13: ["Fernhollow: board returned HTTP 502"]}
    touch = timedelta(minutes=2)
    for index, start in enumerate(recent):
        stamp = clock.utc(start + touch)
        seen, new, total, passed = conn.execute(
            """SELECT
                 SUM(j.first_seen <= :s AND j.last_seen >= :s),
                 SUM(j.first_seen = :s),
                 SUM(j.first_seen <= :s),
                 SUM(j.first_seen <= :s AND j.gate_failed = 0)
               FROM jobs j JOIN companies c ON c.id = j.company_id
               WHERE c.ats_type != 'manual'""",
            {"s": stamp},
        ).fetchone()
        errors = flaky.get(index)
        _insert(
            conn, "sweep_runs", started_at=clock.utc(start + timedelta(seconds=4)),
            finished_at=clock.utc(start + timedelta(minutes=5, seconds=12)),
            companies=company_count, jobs_seen=seen or 0, jobs_new=new or 0,
            errors=json.dumps(errors) if errors else None,
            rescore_total=total or 0, rescore_passed=passed or 0,
            rescore_rejected=(total or 0) - (passed or 0),
        )


def _seed_schedule(conn, script, clock, excused) -> None:
    """Prep blocks marked done on most recent workdays, and one excused day."""
    rng = _rng("schedule")
    for back in range(12, -1, -1):
        day = clock.workday(back)
        coding, prep = rng.random() < 0.8, rng.random() < 0.7
        notes = rng.choice(_CODING_NOTES), rng.choice(_PREP_NOTES)
        if day is None or day == excused or (back == 0 and clock.slot < 2):
            continue
        for done, key, note, hour in ((coding, "coding", notes[0], 15),
                                      (prep, "prep", notes[1], 16)):
            if done:
                _insert(conn, "day_log", day=day.isoformat(), block_key=key, note=note,
                        source="ui", created_at=clock.utc(clock.at(day, hour, 34)))
    _insert(conn, "day_excused", day=excused.isoformat(), reason=script["excused_reason"],
            source="ui", created_at=clock.utc(clock.at(excused, 8, 5)))


def build(out_path: Path, anchor: date, slot: int = 1) -> dict:
    """Write the demo database for an anchor day and slot. Returns a summary."""
    out_path = Path(out_path)
    if out_path.resolve() == db.DB_PATH.resolve():
        raise ValueError("demo_seed never writes to the real database")
    profile = yaml.safe_load(PROFILE_PATH.read_text())
    bank, script = _load("jobs.yaml"), _load("tracker.yaml")
    clock = Clock(anchor, slot)
    sweeps = clock.sweeps()
    excused = clock.workday(script["excused_workday"])

    # Built beside the target and moved into place, so a reader never sees
    # half a database and an existing file is replaced, never opened.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # One scratch file per process: two workers starting together each
    # build their own and the last move wins, with identical content.
    scratch = out_path.with_name(f"{out_path.name}.{os.getpid()}.building")
    scratch.unlink(missing_ok=True)
    conn = db.get_connection(scratch)
    try:
        db.init_db(conn)
        companies = _seed_companies(conn, profile, clock)
        _seed_board_jobs(conn, bank, companies, clock, sweeps)
        apps = _seed_tracker(conn, bank, script, companies, clock, sweeps)
        scored = rescore_all(conn, profile, now=clock.ref.astimezone(timezone.utc))
        mail = _seed_mail(conn, _load("mail.yaml"), apps, profile, clock)
        _seed_dismissals(conn, clock, excused)
        _seed_contacts(conn, _load("people.yaml"), apps, clock)
        _seed_sweep_runs(conn, clock, sweeps, len(companies))
        _seed_schedule(conn, script, clock, excused)
        last_sync = sweeps[-1] + timedelta(minutes=5, seconds=40)
        _insert(conn, "app_state", key="last_email_sync", value=clock.utc(last_sync))
        conn.commit()
    finally:
        conn.close()
    os.replace(scratch, out_path)
    return {"jobs": scored["total"], "passed": scored["passed"],
            "applications": len(apps), "mail": mail}


def main(argv: list[str]) -> None:
    now = datetime.now()
    out = Path(argv[0]) if argv else ROOT / "data" / "demo" / "preview.db"
    summary = build(out, now.date(), slot_for(now))
    print(f"{out}: {summary['jobs']} jobs ({summary['passed']} pass gates), "
          f"{summary['applications']} applications, {len(summary['mail'])} messages")


if __name__ == "__main__":
    main(sys.argv[1:])
