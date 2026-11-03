"""Stage 2 weighted scoring (0-100) for gate survivors.

Pure functions: input is a job dict plus the `scoring:` section of
profile.yaml, output is a ScoreResult whose every component and
multiplier carries a human-readable reason. Deterministic — no LLM, no
randomness, no clock.
"""

import re
from dataclasses import dataclass

from scoring.text import contains_keyword, matching_keywords, required_experience_years

# Title markers, the fallback when the description states no years bar;
# checked in order, first hit wins. Staff/principal/intern titles are
# normally gate-rejected before scoring, but detection stays complete so
# score.py works standalone.
_TITLE_LEVELS = [
    ("intern", ["intern", "internship", "co-op"]),
    ("principal", ["principal", "distinguished", "fellow"]),
    ("staff", ["staff"]),
    ("senior", ["senior", "sr"]),
    ("junior", ["junior", "jr", "entry level", "new grad", "new graduate", "early career"]),
]
# Roman/numeric ladder markers ("Software Engineer II", "Engineer 3,
# Platform"). Roman numerals are case-sensitive: lowercase "ii" is a word
# fragment, uppercase is a level. Digits only count right after
# engineer/developer or at the end of the title — a bare mid-title number
# is usually a team name, not a level.
_LADDER_PATTERNS = [
    (re.compile(r"\b(?:III|IV)\b"), "senior"),
    (re.compile(r"\bII\b"), "mid"),
    (re.compile(r"\bI\b\s*$"), "junior"),
    (re.compile(r"\b(?:engineer|developer)\s+(?:3|4)\b", re.IGNORECASE), "senior"),
    (re.compile(r"\b(?:engineer|developer)\s+2\b", re.IGNORECASE), "mid"),
    (re.compile(r"\b(?:engineer|developer)\s+1\b", re.IGNORECASE), "junior"),
    (re.compile(r"\b(?:3|4)\s*$"), "senior"),
    (re.compile(r"\b2\s*$"), "mid"),
    (re.compile(r"\b1\s*$"), "junior"),
]
_JUNIOR_DESC_PHRASES = ["entry level", "new grad", "new graduate"]
# Fallbacks when scoring.seniority.years_to_level is absent from the
# profile (pre-existing fixtures); the yaml values are the real tunables.
_DEFAULT_JUNIOR_YEARS_MAX = 1
_DEFAULT_SENIOR_YEARS_MIN = 6


@dataclass(frozen=True)
class ScoreResult:
    score: float
    seniority: str
    subtotal: float
    components: list[dict]   # {"reason": str, "points": float, "category": str}
    multipliers: list[dict]  # {"reason": str, "factor": float}
    coverage: dict           # {"required"|"preferred": {"matched": [...], "missing": [...]}}

    def breakdown(self) -> dict:
        """JSON-serializable breakdown for the score_breakdown column."""
        return {
            "score": self.score,
            "subtotal": self.subtotal,
            "seniority": self.seniority,
            "components": self.components,
            "multipliers": self.multipliers,
            "coverage": self.coverage,
        }


def detect_seniority(
    title: str, description: str | None, years_cfg: dict | None = None
) -> tuple[str, str]:
    """Infer level from the description's years-of-experience demand.

    Titles inflate ("Senior" roles asking 3 years) and deflate, so the
    stated years bar decides when present; title markers are only a
    fallback for postings that never state one. Ambiguous means mid.
    """
    cfg = years_cfg or {}
    junior_max = cfg.get("junior_max", _DEFAULT_JUNIOR_YEARS_MAX)
    senior_min = cfg.get("senior_min", _DEFAULT_SENIOR_YEARS_MIN)
    if description:
        years = required_experience_years(description)
        if years is not None:
            if years >= senior_min:
                return "senior", f"description asks for {years}+ years of experience"
            if years <= junior_max:
                return "junior", f"description asks for only {years}+ years of experience"
            return "mid", f"description asks for {years}+ years of experience"
        for phrase in _JUNIOR_DESC_PHRASES:
            if contains_keyword(description, phrase):
                return "junior", f"description mentions '{phrase}'"
    for level, markers in _TITLE_LEVELS:
        for marker in markers:
            if contains_keyword(title, marker):
                return level, f"no years in description; title contains '{marker}'"
    for pattern, level in _LADDER_PATTERNS:
        if match := pattern.search(title):
            return level, f"no years in description; title ladder suffix '{match.group(0)}'"
    return "mid", "no seniority markers — defaulting to mid"


def _normalize_title(title: str) -> str:
    return " ".join(title.split()).casefold()


def score_job(job: dict, scoring_cfg: dict) -> ScoreResult:
    title = job.get("title") or ""
    description = job.get("description") or ""
    text = f"{title}\n{description}"
    components: list[dict] = []

    def add(reason: str, points: float, category: str) -> None:
        components.append({"reason": reason, "points": points, "category": category})

    for target in scoring_cfg["target_titles"]:
        if contains_keyword(title, target):
            add(f"title matches target '{target}'", scoring_cfg["title_match_weight"], "title")
            break
    normalized = _normalize_title(title)
    for target in scoring_cfg["target_titles"]:
        if normalized == _normalize_title(target):
            add(f"exact title match '{target}'", scoring_cfg["exact_title_bonus"], "title")
            break

    # Matched vs missing per keyword class, so the breakdown can show how
    # much of the score is required-stack fit vs nice-to-have padding.
    coverage: dict[str, dict] = {}
    for class_name in ("required", "preferred"):
        keywords = scoring_cfg[f"{class_name}_keywords"]
        matched = matching_keywords(text, keywords)
        coverage[class_name] = {
            "matched": matched,
            "missing": [kw for kw in keywords if kw not in matched],
        }
        weight = scoring_cfg[f"{class_name}_keyword_weight"]
        for kw in matched:
            add(f"{class_name} keyword '{kw}'", weight, class_name)
    for kw in matching_keywords(text, scoring_cfg["negative_keywords"]):
        add(f"negative keyword '{kw}'", scoring_cfg["negative_keyword_weight"], "negative")
    for kw in scoring_cfg["must_haves"] or []:
        if not contains_keyword(text, kw):
            add(f"missing must-have '{kw}'", scoring_cfg["missing_must_have_penalty"], "must-have")

    # Experience-gap penalty: absent config section leaves it off, so the
    # scorer stays usable with pre-existing profiles/fixtures.
    exp_cfg = scoring_cfg.get("experience")
    if exp_cfg:
        required = required_experience_years(description)
        mine = exp_cfg["mine_years"]
        if required is not None and required > mine:
            over = required - mine
            add(
                f"asks for {required} years experience, {over} over mine ({mine})",
                over * exp_cfg["over_penalty_per_year"],
                "experience",
            )

    subtotal = sum(c["points"] for c in components)

    multipliers: list[dict] = []
    level, level_reason = detect_seniority(
        title, description, scoring_cfg["seniority"].get("years_to_level")
    )
    level_factor = scoring_cfg["seniority"]["multipliers"].get(level, 1.0)
    multipliers.append(
        {"reason": f"seniority {level} ({level_reason}) x{level_factor}", "factor": level_factor}
    )
    sparse_cfg = scoring_cfg["sparse_description"]
    if len(description) < sparse_cfg["min_chars"]:
        multipliers.append(
            {
                "reason": (
                    f"sparse description ({len(description)} chars < "
                    f"{sparse_cfg['min_chars']}) x{sparse_cfg['multiplier']}"
                ),
                "factor": sparse_cfg["multiplier"],
            }
        )
    # Optional per-company multiplier from the watchlist (companies[].weight
    # in profile.yaml) — the "this one's an early-stage startup" knob. The
    # caller resolves it; absent or 1.0 adds nothing.
    company_weight = job.get("company_weight")
    if company_weight is not None and company_weight != 1:
        multipliers.append(
            {"reason": f"company weight x{company_weight:g}", "factor": company_weight}
        )

    score = subtotal
    for m in multipliers:
        score *= m["factor"]
    score = round(min(100.0, max(0.0, score)), 1)
    return ScoreResult(
        score=score,
        seniority=level,
        subtotal=subtotal,
        components=components,
        multipliers=multipliers,
        coverage=coverage,
    )
