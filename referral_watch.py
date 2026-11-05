"""Find open roles to send to standing referrals.

Works for any contact marked standing_referral, at whatever company they're
at. The matching rules are pure and live in outreach.py; this module only
gathers the jobs. A role stays listed until it's tied to the contact with
link_contact_to_job or dismissed, and drops off by itself once it's delisted.
"""

import sqlite3
from typing import Mapping

import db
import outreach


def find_matches(
    conn: sqlite3.Connection, cfg: Mapping, years_cfg: Mapping | None
) -> list[dict]:
    """One entry per contact and role, newest role first within each contact."""
    watched = [c for c in db.fetch_contacts(conn) if outreach.is_watching(c, cfg)]
    if not watched:
        return []
    companies = db.fetch_companies(conn)
    matches = []
    for contact in watched:
        company_ids = [
            company["id"] for company in companies
            if outreach.same_company(company["name"], contact["company"], cfg)
        ]
        for job in db.fetch_open_jobs(conn, company_ids, contact["id"]):
            reason = outreach.watch_reason(job, cfg, years_cfg)
            if reason:
                matches.append({
                    "contact_id": contact["id"],
                    "contact_name": contact["name"],
                    "company": job["company"],
                    "job_id": job["id"],
                    "title": job["title"],
                    "location": job["location"],
                    "url": job["url"],
                    "score": job["score"],
                    "first_seen": job["first_seen"],
                    "reason": reason,
                })
    return matches
