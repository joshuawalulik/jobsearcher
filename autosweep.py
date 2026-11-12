"""Automated sweep + rescore, run on a schedule by launchd.

Run as: .venv/bin/python -m autosweep
(installed via launchd/com.jobsearcher.sweep.plist; output appends to
data/sweep.log)

The rescore is not optional: the matches feed only shows scored jobs, so
a sweep without a rescore leaves new postings invisible.
"""

from datetime import datetime, timezone

import contact_sync
import db
import email_sync
from scoring.rescore import rescore_all
from scrapers import sweep


def sweep_and_rescore(conn, profile: dict) -> dict:
    """Sweep all boards, rescore everything, record the run in sweep_runs.

    Shared by the launchd job and the UI sweep button. Returns
    {"sweep": ..., "rescore": ...} (the two summary dicts).
    """
    started = datetime.now(timezone.utc)
    swept = sweep.run(conn, profile)
    rescored = rescore_all(conn, profile, now=datetime.now(timezone.utc))
    db.record_sweep_run(
        conn,
        started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
        companies=swept["companies"],
        jobs_seen=swept["seen"],
        jobs_new=swept["new"],
        errors=swept["errors"],
        rescore_total=rescored["total"],
        rescore_passed=rescored["passed"],
        rescore_rejected=rescored["rejected"],
    )
    return {"sweep": swept, "rescore": rescored}


def main() -> None:
    started = datetime.now(timezone.utc)
    print(f"=== autosweep {started.isoformat(timespec='seconds')} ===", flush=True)
    profile = sweep.load_profile()
    conn = db.get_connection()
    try:
        db.init_db(conn)
        summary = sweep_and_rescore(conn, profile)
        print(sweep.format_summary(summary["sweep"]), flush=True)
        rescored = summary["rescore"]
        print(
            f"Rescored {rescored['total']} jobs: {rescored['passed']} passed gates, "
            f"{rescored['rejected']} rejected.",
            flush=True,
        )
        _email_sync(conn, profile)
        _contact_sync(conn, profile)
    finally:
        conn.close()


def _email_sync(conn, profile: dict) -> None:
    """Inbox → tracker suggestions after the sweep; never fatal, and
    silently skipped until config/secrets.yaml exists."""
    secrets = email_sync.load_secrets()
    if not (profile.get("email") or {}).get("enabled") or secrets is None:
        return
    try:
        print(email_sync.format_summary(email_sync.sync(conn, profile, secrets=secrets)), flush=True)
    except Exception as exc:
        print(f"Email sync failed: {exc}", flush=True)


def _contact_sync(conn, profile: dict) -> None:
    """Contacts' mail → the interaction log; never fatal, and silently
    skipped until config/secrets.yaml exists."""
    secrets = email_sync.load_secrets()
    if not (profile.get("contact_sync") or {}).get("enabled") or secrets is None:
        return
    try:
        print(contact_sync.format_summary(contact_sync.sync(conn, profile, secrets=secrets)), flush=True)
    except Exception as exc:
        print(f"Contact mail sync failed: {exc}", flush=True)


if __name__ == "__main__":
    main()
