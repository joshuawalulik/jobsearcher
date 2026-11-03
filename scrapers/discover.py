"""Resolve company names to ATS type + slug by probing the JSON APIs.

Run as:
    python -m scrapers.discover "Company One" "Company Two" ...
    python -m scrapers.discover --add "Company One" ...

For each name, slug candidates are probed against the Greenhouse, Lever
and Ashby board APIs (the same public endpoints the sweep uses, with the
same politeness delay). A hit reports the ATS, slug and live job count;
--add appends verified hits to config/profile.yaml via the
comment-preserving editor. Companies already on the watchlist (matching
slug, or matching name case-insensitively) are skipped.

A board that resolves but lists zero jobs is reported and NOT treated as
a hit: some ATSes answer unknown slugs with an empty board, so an empty
result can't be distinguished from a wrong slug.
"""

import argparse
import re
import time

import profile_io
from scrapers import ashby, common, greenhouse, lever
from scrapers.sweep import load_profile

ADAPTERS = (greenhouse, lever, ashby)


def slug_candidates(name: str) -> list[str]:
    """Likely board slugs for a company name, most common shapes first."""
    cleaned = re.sub(r"[^a-z0-9 ]", "", name.lower().replace("&", " and "))
    words = cleaned.split()
    candidates = ["".join(words), "-".join(words)]
    if words and words[-1] in ("inc", "labs", "co", "corp", "health"):
        trimmed = words[:-1]
        candidates += ["".join(trimmed), "-".join(trimmed)]
    if len(words) > 1:
        candidates.append(words[0])
    seen: set[str] = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def probe_company(
    name: str, *, user_agent: str, timeout: float, delay: float = 1.0
) -> dict | None:
    """First (ats, slug) whose board answers with listed jobs, or None.

    Tries every adapter for each slug candidate, sleeping `delay` between
    requests like the sweep does.
    """
    for slug in slug_candidates(name):
        for adapter in ADAPTERS:
            try:
                raw = adapter.fetch(slug, user_agent=user_agent, timeout=timeout)
                count = len(adapter.normalize(raw, slug))
            except common.FetchError:
                count = -1  # not found / unusable payload
            finally:
                time.sleep(delay)
            if count > 0:
                return {"name": name, "ats": adapter.ATS_TYPE, "slug": slug, "jobs": count}
    return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="+", help="company names to resolve")
    parser.add_argument(
        "--add", action="store_true",
        help="append verified hits to config/profile.yaml",
    )
    args = parser.parse_args(argv)

    # Resolve the path at call time (not via default args) so tests can
    # point profile_io.PROFILE_PATH at a copy.
    profile = load_profile(profile_io.PROFILE_PATH)
    scraper_cfg = profile["scraper"]
    watched_slugs = {(e["ats"], e["slug"]) for e in profile["companies"]}
    watched_names = {e["name"].casefold() for e in profile["companies"]}

    found: list[dict] = []
    for name in args.names:
        if name.casefold() in watched_names:
            print(f"  skip   | {name} — already on the watchlist")
            continue
        hit = probe_company(
            name,
            user_agent=scraper_cfg["user_agent"],
            timeout=scraper_cfg["timeout_seconds"],
            delay=scraper_cfg["delay_seconds"],
        )
        if hit is None:
            print(f"  miss   | {name} — no greenhouse/lever/ashby board found")
        elif (hit["ats"], hit["slug"]) in watched_slugs:
            print(f"  skip   | {name} — slug {hit['slug']!r} already watched")
        else:
            print(f"  found  | {name} — {hit['ats']}/{hit['slug']} ({hit['jobs']} jobs)")
            found.append(hit)

    if not found:
        print("Nothing new found.")
        return
    if not args.add:
        print(f"{len(found)} new board(s) found. Re-run with --add to append them.")
        return

    doc = profile_io.load_round_trip(profile_io.PROFILE_PATH)
    entries = [
        dict(e)  # keep every field (incl. optional weight) of existing entries
        for e in profile_io.get_value(doc, "companies")
    ] + [{"name": h["name"], "ats": h["ats"], "slug": h["slug"]} for h in found]
    profile_io.apply_edits(doc, {"companies": entries})
    profile_io.save_round_trip(doc, profile_io.PROFILE_PATH)
    print(f"Added {len(found)} companies to profile.yaml — run a sweep to pull their jobs.")


if __name__ == "__main__":
    main()
