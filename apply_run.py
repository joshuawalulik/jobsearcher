"""Batch application runs: stage top matches, then mark them applied.

Run as:
    python -m apply_run list [N]          # top N untracked matches (default 5)
    python -m apply_run start JOB_ID ...  # track + create application folders
    python -m apply_run ready APP_ID ...  # resume built, not yet submitted
    python -m apply_run applied APP_ID ...# move to applied after submitting
    python -m apply_run skip APP_ID ...   # Stage 2 Skip verdict: move to skipped

`start` tracks each job (considering lane, normal event logging) and
stages <workflow.home>/Applications/<company>/<role>/ — the existing
per-company-then-role layout of the user's job-search folder (e.g.
acme/"backend engineer"; workflow.home in profile.yaml). The company
folder is matched against existing directories by normalized name so
near-name variants are reused rather than duplicated; the role
folder is the job title. job_<app_id>.md (posting link, score
breakdown, full JD) is the input the /apply skill tailors a resume
against, and `start` also reports the next free resume version for the
company (scanning the whole company tree, so historical versions keep
the counter honest). `applied` is the "I submitted it" step: first move
into applied stamps applied_at (db.set_application_lane).

Deterministic staging only — tailoring happens in the skill, not here.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import db


def applications_dir(profile: dict | None = None) -> Path:
    """<workflow.home>/Applications from profile.yaml, repo-local fallback."""
    if profile is None:
        from scrapers.sweep import load_profile  # late: keeps import-time deps thin
        profile = load_profile()
    home = (profile.get("workflow") or {}).get("home")
    root = Path(home) if home else Path(__file__).parent / "ResumesAndJobSearch"
    return root / "Applications"


def top_untracked(conn, n: int) -> list[dict]:
    """Best-scored gate-passing, non-dismissed, untracked matches."""
    rows = [dict(r) for r in db.fetch_matches(conn) if r["tracked"] is None]
    return rows[:n]


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\- ,]", "", name).strip() or "unknown"


def find_company_dir(root: Path, company: str) -> Path:
    """The existing folder for a company, tolerating name variants.

    Matches by lowercased alphanumeric comparison with containment in
    either direction, so "globex" serves company "Globex Corp" and
    "acme co" serves "AcmeCo". Shortest match wins (the oldest
    folders tend to be the informal short names). Falls back to a new
    folder named after the company.
    """
    norm = re.sub(r"[^a-z0-9]", "", company.lower())
    candidates = []
    if root.exists() and norm:
        for d in root.iterdir():
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            dn = re.sub(r"[^a-z0-9]", "", d.name.lower())
            if dn and (norm.startswith(dn) or dn.startswith(norm)):
                candidates.append(d)
    if candidates:
        return min(candidates, key=lambda d: len(d.name))
    return root / _safe_name(company)


def next_resume_version(company_dir: Path) -> int:
    """Next free _v<N> across the whole company tree (per-company
    counter per the master bank; historical versions count)."""
    pat = re.compile(r"_v(\d+)\.[A-Za-z]+$")
    highest = 0
    if company_dir.exists():
        for f in company_dir.rglob("Joshua_Walulik_Resume*"):
            m = pat.search(f.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def start_job(conn, job_id: int, root: Path) -> dict:
    """Track one job and stage its company folder.

    Reuses the existing application if the job is already tracked (the
    job file is (re)written either way). Raises ValueError for unknown ids.
    """
    row = conn.execute(
        """
        SELECT j.id, j.title, j.location, j.url, j.score, j.score_breakdown,
               j.description, c.name AS company, c.ats_type, c.slug,
               (SELECT a.id FROM applications a WHERE a.job_id = j.id) AS app_id
        FROM jobs j JOIN companies c ON c.id = j.company_id WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no job with id {job_id}")
    app_id = row["app_id"] or db.create_application(conn, job_id)

    # A tracked job always carries a score (same rule as the app's track
    # route); gate-failed jobs arrive unscored, so score them here.
    row = dict(row)
    if row["score"] is None:
        from scoring.score import score_job
        from scrapers.sweep import load_profile
        profile = load_profile()
        weight = next(
            (e.get("weight") for e in profile["companies"]
             if e["ats"] == row["ats_type"] and e["slug"] == row["slug"]),
            None,
        )
        result = score_job(
            {"title": row["title"], "description": row["description"],
             "company_weight": weight},
            profile["scoring"],
        )
        row["score"] = result.score
        row["score_breakdown"] = json.dumps(result.breakdown())
        conn.execute(
            "UPDATE jobs SET score = ?, score_breakdown = ? WHERE id = ?",
            (row["score"], row["score_breakdown"], job_id),
        )
        conn.commit()

    company_dir = find_company_dir(root, row["company"])
    folder = company_dir / _safe_name(row["title"])
    folder.mkdir(parents=True, exist_ok=True)
    breakdown = ""
    if row["score_breakdown"]:
        breakdown = json.dumps(json.loads(row["score_breakdown"]), indent=2)
    (folder / f"job_{app_id}.md").write_text(
        f"# {row['title']} — {row['company']}\n\n"
        f"- app_id: {app_id}\n"
        f"- job_id: {row['id']}\n"
        f"- location: {row['location'] or '?'}\n"
        f"- score: {row['score']}\n"
        f"- apply here: {row['url'] or 'no url'}\n\n"
        f"## Score breakdown\n\n```json\n{breakdown}\n```\n\n"
        f"## Job description\n\n{row['description'] or '(none saved)'}\n"
    )
    compact = re.sub(r"[^A-Za-z0-9]", "", row["company"]) or "Company"
    version = next_resume_version(company_dir)
    return {"app_id": app_id, "job_id": row["id"], "company": row["company"],
            "title": row["title"], "url": row["url"], "folder": folder,
            "basename": f"Joshua_Walulik_Resume_{compact}_v{version}"}


def mark_ready(conn, app_ids: list[int]) -> list[int]:
    """Move each application to ready (resume rendered, not yet submitted).
    resume_build does this automatically; the command is the manual path."""
    return [a for a in app_ids if db.set_application_lane(conn, a, "ready")]


def mark_applied(conn, app_ids: list[int]) -> list[int]:
    """Move each application to applied; returns the ids that existed."""
    return [a for a in app_ids if db.set_application_lane(conn, a, "applied")]


def mark_skipped(conn, app_ids: list[int]) -> list[int]:
    """Move each application to skipped (Stage 2 Skip verdict)."""
    return [a for a in app_ids if db.set_application_lane(conn, a, "skipped")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("n", nargs="?", type=int, default=5)
    p_start = sub.add_parser("start")
    p_start.add_argument("job_ids", nargs="+", type=int)
    p_ready = sub.add_parser("ready")
    p_ready.add_argument("app_ids", nargs="+", type=int)
    p_applied = sub.add_parser("applied")
    p_applied.add_argument("app_ids", nargs="+", type=int)
    p_skip = sub.add_parser("skip")
    p_skip.add_argument("app_ids", nargs="+", type=int)
    args = ap.parse_args()

    conn = db.get_connection()
    try:
        db.init_db(conn)
        if args.cmd == "list":
            for row in top_untracked(conn, args.n):
                print(f"  job {row['id']:>6}  score {row['score']:>5g}  "
                      f"{row['company']} — {row['title']}")
        elif args.cmd == "start":
            root = applications_dir()
            for job_id in args.job_ids:
                info = start_job(conn, job_id, root)
                print(f"  app {info['app_id']}: {info['company']} — {info['title']}\n"
                      f"      folder:   {info['folder']}\n"
                      f"      basename: {info['basename']}\n"
                      f"      apply:    {info['url'] or 'no url'}")
        elif args.cmd in ("ready", "applied", "skip"):
            movers = {"ready": mark_ready, "applied": mark_applied, "skip": mark_skipped}
            lane = "skipped" if args.cmd == "skip" else args.cmd
            moved = movers[args.cmd](conn, args.app_ids)
            missing = set(args.app_ids) - set(moved)
            print(f"  moved to {lane}: {moved or 'none'}")
            if missing:
                print(f"  not found: {sorted(missing)}", file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
