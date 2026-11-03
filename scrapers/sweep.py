"""Sweep command: sync companies from profile.yaml, fetch boards, upsert jobs.

Run as: python -m scrapers.sweep
"""

import time
from pathlib import Path

import yaml

import db
from scrapers import ashby, common, greenhouse, lever

PROFILE_PATH = Path(__file__).parent.parent / "config" / "profile.yaml"

ADAPTERS = {mod.ATS_TYPE: mod for mod in (greenhouse, lever, ashby)}


def load_profile(path: Path = PROFILE_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def sync_companies(conn, companies: list[dict]) -> None:
    """Mirror the yaml watchlist into the companies table.

    Entries present in yaml are upserted and marked active; rows no longer
    in yaml are marked inactive (never deleted — their jobs age out).
    """
    for entry in companies:
        if entry.get("ats") not in ADAPTERS:
            raise ValueError(
                f"company {entry.get('name')!r}: unknown ats {entry.get('ats')!r} "
                f"(expected one of {sorted(ADAPTERS)})"
            )
    for entry in companies:
        conn.execute(
            """
            INSERT INTO companies (name, ats_type, slug) VALUES (?, ?, ?)
            ON CONFLICT (ats_type, slug) DO UPDATE SET
                name = excluded.name,
                active = 1
            """,
            (entry["name"], entry["ats"], entry["slug"]),
        )
    keys = [(entry["ats"], entry["slug"]) for entry in companies]
    for row in conn.execute("SELECT id, ats_type, slug FROM companies").fetchall():
        if (row["ats_type"], row["slug"]) not in keys:
            conn.execute("UPDATE companies SET active = 0 WHERE id = ?", (row["id"],))
    conn.commit()


def upsert_jobs(conn, company_id: int, jobs: list[dict]) -> int:
    """Insert new jobs, refresh existing ones, bump last_seen. Returns new-row count.

    Deduped by external_id (first occurrence wins) — a same-second re-upsert
    reads as new via RETURNING, so duplicates in one payload would double-count.
    """
    new = 0
    seen_ids: set[str] = set()
    for job in jobs:
        if job["external_id"] in seen_ids:
            continue
        seen_ids.add(job["external_id"])
        cursor = conn.execute(
            """
            INSERT INTO jobs (company_id, external_id, title, location, department,
                              url, description, posted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (company_id, external_id) DO UPDATE SET
                title       = excluded.title,
                location    = excluded.location,
                department  = excluded.department,
                url         = excluded.url,
                description = excluded.description,
                posted_at   = excluded.posted_at,
                last_seen   = datetime('now')
            RETURNING first_seen = last_seen AS is_new
            """,
            (
                company_id,
                job["external_id"],
                job["title"],
                job["location"],
                job["department"],
                job["url"],
                job["description"],
                job["posted_at"],
            ),
        )
        if cursor.fetchone()["is_new"]:
            new += 1
    conn.commit()
    return new


def sweep_company(conn, company, *, user_agent: str, timeout: float) -> tuple[int, int]:
    """Fetch, cache, normalize, and upsert one company's board.

    Returns (jobs_seen, jobs_new). Raises common.FetchError on failure.
    """
    adapter = ADAPTERS[company["ats_type"]]
    raw = adapter.fetch(company["slug"], user_agent=user_agent, timeout=timeout)
    common.write_cache(company["ats_type"], company["slug"], raw)
    jobs = adapter.normalize(raw, company["slug"])
    new = upsert_jobs(conn, company["id"], jobs)
    return len(jobs), new


def run(conn, profile: dict) -> dict:
    """Sweep every active watchlist board into the given connection.

    Per-company progress goes to stdout (the launchd log); the returned
    summary dict {companies, seen, new, errors} is for callers that need
    the numbers (autosweep, the UI sweep button).
    """
    scraper_cfg = profile["scraper"]
    delay = scraper_cfg["delay_seconds"]
    user_agent = scraper_cfg["user_agent"]
    timeout = scraper_cfg["timeout_seconds"]

    sync_companies(conn, profile.get("companies") or [])

    active = conn.execute(
        "SELECT id, name, ats_type, slug FROM companies WHERE active = 1 ORDER BY name"
    ).fetchall()

    total_seen = total_new = 0
    errors: list[str] = []
    for i, company in enumerate(active):
        if i > 0:
            time.sleep(delay)
        try:
            seen, new = sweep_company(conn, company, user_agent=user_agent, timeout=timeout)
            total_seen += seen
            total_new += new
            print(f"  {company['name']}: {seen} jobs ({new} new)")
        except common.BoardNotFound:
            msg = (
                f"{company['name']}: board not found — check slug "
                f"{company['slug']!r} for ats {company['ats_type']!r}"
            )
            print(f"  {msg}")
            errors.append(msg)
        except common.FetchError as exc:
            msg = f"{company['name']}: {exc}"
            print(f"  {msg}")
            errors.append(msg)
        except Exception as exc:  # one broken company must never abort the sweep
            msg = f"{company['name']}: unexpected error: {exc}"
            print(f"  {msg}")
            errors.append(msg)

    return {"companies": len(active), "seen": total_seen, "new": total_new, "errors": errors}


def format_summary(summary: dict) -> str:
    line = (
        f"Swept {summary['companies']} companies: "
        f"{summary['seen']} jobs ({summary['new']} new)"
    )
    errors = summary["errors"]
    if errors:
        line += f", {len(errors)} error{'s' if len(errors) > 1 else ''} ({'; '.join(errors)})"
    return line


def main() -> None:
    profile = load_profile()
    conn = db.get_connection()
    db.init_db(conn)
    print(format_summary(run(conn, profile)))


if __name__ == "__main__":
    main()
