"""Rescore command: run gates + scorer over every job in the DB.

Run as: python -m scoring.rescore

Applies the current config/profile.yaml to all jobs so profile edits take
effect instantly. Gate-rejected jobs store their reasons in gate_reason
and get a NULL score; survivors store score + JSON breakdown.
"""

import json
from collections import Counter
from datetime import datetime, timezone

import db
from scoring.gates import GateResult, check_gates
from scoring.score import score_job
from scrapers.sweep import load_profile


def rescore_all(conn, profile: dict, *, now: datetime) -> dict:
    """Rescore every job in one transaction. Returns summary counts."""
    gates_cfg = profile["gates"]
    scoring_cfg = profile["scoring"]
    # Optional per-company score multipliers from the watchlist.
    company_weights = {
        (entry["ats"], entry["slug"]): entry["weight"]
        for entry in profile.get("companies") or []
        if "weight" in entry
    }
    # Manually-added jobs (ats_type 'manual') are scored like any other
    # so profile edits keep their fit number fresh, but they skip gates:
    # gates exist to drop scraping noise, and a hand-added job is wanted
    # by definition. The feed excludes them regardless (tracker-only).
    jobs = conn.execute(
        """
        SELECT j.id, j.title, j.location, j.description, j.posted_at, j.last_seen,
               c.ats_type = 'manual' AS is_manual,
               c.ats_type AS company_ats, c.slug AS company_slug,
               EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)
                   AS is_tracked
        FROM jobs j JOIN companies c ON c.id = j.company_id
        """
    ).fetchall()

    rejections_by_gate: Counter[str] = Counter()
    passed = 0
    for job in jobs:
        job_dict = dict(job)
        is_tracked = job_dict.pop("is_tracked")
        job_dict["company_weight"] = company_weights.get(
            (job_dict.pop("company_ats"), job_dict.pop("company_slug"))
        )
        gate = (
            GateResult(passed=True)
            if job_dict.pop("is_manual")
            else check_gates(job_dict, gates_cfg, now=now)
        )
        if not gate.passed:
            for reason in gate.reasons:
                rejections_by_gate[reason.split(":", 1)[0]] += 1
            if is_tracked:
                # A tracked job keeps a fresh fit score even when
                # gate-rejected — gates decide feed entry, not whether a
                # job the user chose deserves a number.
                result = score_job(job_dict, scoring_cfg)
                conn.execute(
                    """UPDATE jobs SET gate_failed = 1, gate_reason = ?,
                       score = ?, score_breakdown = ? WHERE id = ?""",
                    ("; ".join(gate.reasons), result.score,
                     json.dumps(result.breakdown()), job_dict["id"]),
                )
            else:
                conn.execute(
                    """UPDATE jobs SET gate_failed = 1, gate_reason = ?,
                       score = NULL, score_breakdown = NULL WHERE id = ?""",
                    ("; ".join(gate.reasons), job_dict["id"]),
                )
            continue
        passed += 1
        result = score_job(job_dict, scoring_cfg)
        conn.execute(
            """UPDATE jobs SET gate_failed = 0, gate_reason = NULL,
               score = ?, score_breakdown = ? WHERE id = ?""",
            (result.score, json.dumps(result.breakdown()), job_dict["id"]),
        )
    conn.commit()
    return {
        "total": len(jobs),
        "passed": passed,
        "rejected": len(jobs) - passed,
        "rejections_by_gate": dict(rejections_by_gate),
    }


def main() -> None:
    profile = load_profile()
    conn = db.get_connection()
    db.init_db(conn)
    summary = rescore_all(conn, profile, now=datetime.now(timezone.utc))
    print(
        f"Rescored {summary['total']} jobs: {summary['passed']} scored, "
        f"{summary['rejected']} gate-rejected"
    )
    if summary["rejections_by_gate"]:
        print("Rejections by gate (a job can fail several):")
        for gate, count in sorted(
            summary["rejections_by_gate"].items(), key=lambda kv: -kv[1]
        ):
            print(f"  {gate}: {count}")


if __name__ == "__main__":
    main()
