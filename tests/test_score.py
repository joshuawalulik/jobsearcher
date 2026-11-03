"""Tests for stage-2 weighted scoring and seniority detection."""

import pytest

from scoring.score import detect_seniority, score_job


@pytest.fixture
def cfg():
    return {
        "target_titles": ["Software Engineer", "Full Stack Engineer", "Frontend Engineer"],
        "title_match_weight": 30,
        "exact_title_bonus": 15,
        "required_keywords": ["react", "typescript", "node", "javascript", "api"],
        "required_keyword_weight": 5,
        "preferred_keywords": ["graphql", "aws", "ai", "java", "full stack"],
        "preferred_keyword_weight": 2,
        "negative_keywords": ["php", "wordpress", "c++"],
        "negative_keyword_weight": -10,
        "must_haves": [],
        "missing_must_have_penalty": -20,
        "seniority": {
            "target": "mid",
            "multipliers": {
                "intern": 0.2, "junior": 0.75, "mid": 1.0,
                "senior": 0.7, "staff": 0.4, "principal": 0.3,
            },
        },
        "sparse_description": {"min_chars": 400, "multiplier": 0.85},
    }


def job(**overrides) -> dict:
    base = {
        "title": "Software Engineer",
        "description": "Build products with react, typescript and graphql. " * 10,
    }
    return {**base, **overrides}


# --- seniority detection ----------------------------------------------------

@pytest.mark.parametrize(
    "title, level",
    [
        ("Software Engineering Intern", "intern"),
        ("Principal Engineer", "principal"),
        ("Staff Software Engineer", "staff"),
        ("Senior Software Engineer", "senior"),
        ("Sr. Backend Software Engineer", "senior"),
        ("Junior Developer", "junior"),
        ("Software Engineer, New Grad", "junior"),
        ("Software Engineer II", "mid"),
        ("Software Engineer III", "senior"),
        ("Software Engineer I", "junior"),
        ("Software Engineer 2", "mid"),
        ("Software Engineer 3, Documentation Platform", "senior"),
        ("Engineer, Area 120 Team", "mid"),  # mid-title number is a team, not a level
        ("Software Engineer", "mid"),
    ],
)
def test_detect_seniority_from_title(title, level):
    # Title markers only apply when the description states no years bar.
    assert detect_seniority(title, "")[0] == level


def test_seniority_years_override_senior_title():
    # An inflated "Senior" title asking mid-level years scores as mid.
    level, reason = detect_seniority(
        "Senior Software Engineer", "You have 3+ years of professional experience."
    )
    assert level == "mid"
    assert "3+ years" in reason


def test_seniority_years_override_junior_title():
    level, _ = detect_seniority(
        "Junior Developer", "Requires 7+ years of experience."
    )
    assert level == "senior"


def test_seniority_low_years_reads_junior():
    level, reason = detect_seniority(
        "Software Engineer", "1+ years of professional experience."
    )
    assert level == "junior"
    assert "only 1+ years" in reason


def test_seniority_years_bands_tunable():
    cfg = {"junior_max": 2, "senior_min": 5}
    assert detect_seniority("Engineer", "2+ years of experience.", cfg)[0] == "junior"
    assert detect_seniority("Engineer", "5+ years of experience.", cfg)[0] == "senior"


def test_seniority_from_description_years():
    level, reason = detect_seniority(
        "Software Engineer", "We need 8+ years of software engineering experience."
    )
    assert level == "senior"
    assert "8+ years" in reason


def test_seniority_mid_years_stays_mid():
    assert detect_seniority("Software Engineer", "2+ years of experience.")[0] == "mid"


def test_seniority_entry_level_description():
    assert detect_seniority("Software Engineer", "This is an entry level role.")[0] == "junior"


def test_seniority_default_is_mid_with_reason():
    level, reason = detect_seniority("Software Engineer", "")
    assert level == "mid"
    assert "defaulting to mid" in reason


def test_seniority_not_fooled_by_substrings():
    # "internal" is not "intern", "sroute" is not "sr"
    assert detect_seniority("Engineer, Internal Tools", "")[0] == "mid"


# --- scoring components -----------------------------------------------------

def test_title_match_and_exact_bonus(cfg):
    result = score_job(job(title="Software Engineer", description=""), cfg)
    reasons = [c["reason"] for c in result.components]
    assert "title matches target 'Software Engineer'" in reasons
    assert "exact title match 'Software Engineer'" in reasons


def test_title_substring_match_without_exact_bonus(cfg):
    result = score_job(job(title="Software Engineer, Payments", description=""), cfg)
    reasons = [c["reason"] for c in result.components]
    assert "title matches target 'Software Engineer'" in reasons
    assert not any("exact title" in r for r in reasons)


def test_title_match_awarded_once_despite_overlapping_targets(cfg):
    result = score_job(job(title="Full Stack Frontend Engineer", description=""), cfg)
    title_hits = [c for c in result.components if "matches target" in c["reason"]]
    assert len(title_hits) == 1


def test_keyword_weights_and_reasons(cfg):
    description = "We use react, aws, and php. " + "x" * 400
    result = score_job(job(title="Backend Developer", description=description), cfg)
    points = {c["reason"]: c["points"] for c in result.components}
    assert points["required keyword 'react'"] == 5
    assert points["preferred keyword 'aws'"] == 2
    assert points["negative keyword 'php'"] == -10
    assert result.subtotal == -3


def test_keywords_use_word_boundaries(cfg):
    description = "Maintain javascript apps. " + "x" * 400  # no "ai", no "java"
    result = score_job(job(title="Backend Developer", description=description), cfg)
    reasons = [c["reason"] for c in result.components]
    assert "required keyword 'javascript'" in reasons
    assert "preferred keyword 'ai'" not in reasons
    assert "preferred keyword 'java'" not in reasons


def test_keywords_found_in_title_too(cfg):
    result = score_job(job(title="React Engineer", description="x" * 400), cfg)
    assert "required keyword 'react'" in [c["reason"] for c in result.components]


def test_missing_must_have_penalty(cfg):
    cfg["must_haves"] = ["react", "kubernetes"]
    result = score_job(job(), cfg)  # description has react, not kubernetes
    points = {c["reason"]: c["points"] for c in result.components}
    assert points["missing must-have 'kubernetes'"] == -20
    assert "missing must-have 'react'" not in points


# --- multipliers and clamping -----------------------------------------------

def test_seniority_multiplier_applied(cfg):
    mid = score_job(job(title="Software Engineer"), cfg)
    senior = score_job(job(title="Senior Software Engineer"), cfg)
    assert senior.subtotal == mid.subtotal - cfg["exact_title_bonus"]
    assert senior.score == round(senior.subtotal * 0.7, 1)
    assert any("seniority senior" in m["reason"] for m in senior.multipliers)


def test_sparse_description_haircut(cfg):
    result = score_job(job(description="react and typescript."), cfg)
    assert any("sparse description" in m["reason"] for m in result.multipliers)
    assert result.score == round(result.subtotal * 1.0 * 0.85, 1)


def test_full_description_no_haircut(cfg):
    result = score_job(job(), cfg)
    assert not any("sparse" in m["reason"] for m in result.multipliers)


def test_score_clamped_to_zero(cfg):
    result = score_job(
        job(title="Backend Developer", description="php wordpress c++ " + "x" * 400), cfg
    )
    assert result.subtotal == -30
    assert result.score == 0.0


def test_score_clamped_to_hundred(cfg):
    cfg["title_match_weight"] = 500
    assert score_job(job(), cfg).score == 100.0


def test_deterministic(cfg):
    assert score_job(job(), cfg) == score_job(job(), cfg)


def test_breakdown_is_json_shape(cfg):
    breakdown = score_job(job(), cfg).breakdown()
    assert set(breakdown) == {
        "score", "subtotal", "seniority", "components", "multipliers", "coverage",
    }
    assert all({"reason", "points", "category"} <= set(c) for c in breakdown["components"])
    assert all({"reason", "factor"} <= set(m) for m in breakdown["multipliers"])
    assert set(breakdown["coverage"]) == {"required", "preferred"}


def test_components_carry_categories(cfg):
    description = "We use react, aws, and php. Needs 9 years of experience. " + "x" * 400
    cfg["experience"] = {"mine_years": 3, "over_penalty_per_year": -6}
    cfg["must_haves"] = ["kubernetes"]
    result = score_job(job(title="Software Engineer", description=description), cfg)
    by_category = {c["category"] for c in result.components}
    assert by_category == {"title", "required", "preferred", "negative", "must-have", "experience"}


def test_coverage_lists_matched_and_missing(cfg):
    description = "We use react and typescript. " + "x" * 400
    result = score_job(job(title="Backend Developer", description=description), cfg)
    assert result.coverage["required"]["matched"] == ["react", "typescript"]
    assert result.coverage["required"]["missing"] == ["node", "javascript", "api"]
    assert result.coverage["preferred"]["matched"] == []
    assert len(result.coverage["preferred"]["missing"]) == 5


# --- experience-gap penalty --------------------------------------------------

def test_experience_penalty_per_year_over(cfg):
    cfg["experience"] = {"mine_years": 3, "over_penalty_per_year": -6}
    result = score_job(
        job(description="Needs 5+ years of experience building web apps with react. " * 10),
        cfg,
    )
    component = next(c for c in result.components if "experience" in c["reason"])
    assert component["reason"] == "asks for 5 years experience, 2 over mine (3)"
    assert component["points"] == -12


def test_experience_no_penalty_at_or_below_mine(cfg):
    cfg["experience"] = {"mine_years": 3, "over_penalty_per_year": -6}
    result = score_job(
        job(description="Needs 3+ years of experience with react and node. " * 10), cfg
    )
    assert not any("over mine" in c["reason"] for c in result.components)


def test_experience_penalty_off_when_config_absent(cfg):
    result = score_job(
        job(description="Needs 5+ years of experience with react. " * 10), cfg
    )
    assert not any("over mine" in c["reason"] for c in result.components)


# --- company weight and startup markers --------------------------------------

def test_company_weight_multiplier_applied(cfg):
    weighted = score_job(job(company_weight=0.8), cfg)
    unweighted = score_job(job(), cfg)
    assert weighted.score == round(unweighted.score * 0.8, 1)
    assert any("company weight x0.8" in m["reason"] for m in weighted.multipliers)


def test_company_weight_absent_or_one_adds_nothing(cfg):
    for j in (job(), job(company_weight=None), job(company_weight=1.0)):
        assert not any("company weight" in m["reason"] for m in score_job(j, cfg).multipliers)


def test_startup_fluff_at_big_companies_not_penalized(cfg):
    # Funding-stage phrases penalize; "startup-like environment" fluff must not.
    cfg["negative_keywords"] = ["early stage", "seed stage", "series a", "founding engineer"]
    fluff = job(description=(
        "We operate with a startup mindset in a startup-like environment, "
        "moving fast at enterprise scale with react. " * 5
    ))
    result = score_job(fluff, cfg)
    assert not any(c["category"] == "negative" for c in result.components)
    real = job(description="We are an early-stage, seed stage company. Series A closed. " * 8)
    hits = [c for c in score_job(real, cfg).components if c["category"] == "negative"]
    assert len(hits) == 3  # early stage, seed stage, series a
