"""Tests for word-boundary keyword matching (the profile.yaml edge cases)."""

import pytest

from scoring.text import contains_keyword, matching_keywords, required_experience_years


@pytest.mark.parametrize(
    "text, keyword",
    [
        ("We use AI daily", "ai"),
        ("AI/ML pipelines", "ai"),
        ("Java 21 backend", "java"),
        ("REST APIs everywhere", "rest"),
        ("ASP.NET services", ".net"),
        (".NET framework", ".net"),
        ("Modern C++ codebase", "c++"),
        ("CI/CD pipelines", "ci/cd"),
        ("full-stack engineer", "full stack"),
        ("full stack engineer", "full stack"),
        ("node.js services", "node"),
        ("Experience with Node.JS", "node.js"),
        ("Requires TS/SCI eligibility", "TS/SCI"),
        ("QA Engineer", "qa"),
    ],
)
def test_matches(text, keyword):
    assert contains_keyword(text, keyword)


@pytest.mark.parametrize(
    "text, keyword",
    [
        ("maintain the platform", "ai"),
        ("javascript expert", "java"),
        ("restaurant delivery app", "rest"),
        ("networking stack", ".net"),
        ("wide.network domains", ".net"),
        ("works with bios and kiosks", "ios"),
        ("apiary management", "api"),
        ("scapegoat", "ape"),
    ],
)
def test_non_matches(text, keyword):
    assert not contains_keyword(text, keyword)


def test_ios_matches_as_word():
    assert contains_keyword("Senior iOS Engineer", "ios")


def test_matching_keywords_preserves_profile_order_and_dedupes_nothing():
    found = matching_keywords("react and typescript, no php", ["typescript", "react", "node"])
    assert found == ["typescript", "react"]


@pytest.mark.parametrize(
    "text, years",
    [
        ("5+ years of experience with React", 5),
        ("At least 7 years of professional software experience", 7),
        ("3+ years React, 5+ years of backend experience", 3),  # lowest bar wins
        ("2-4 years relevant experience", 2),  # range reads as lower bound
        ("2 to 4 years of experience", 2),
        ("12 yrs experience? no. 2 years experience", 2),
        # Tiered alternative qualifications (the Microsoft pattern): the
        # entry bar is the requirement, not the Master's-equivalent tier.
        ("Bachelor's AND 2+ years technical engineering experience. "
         "Preferred: 4+ years technical engineering experience. "
         "OR Master's AND 10+ years technical engineering experience", 2),
        ("years of experience", None),  # no number
        ("Founded 12 years ago. Experience with React required", None),  # sentence boundary
        ("", None),
        # Technology-scoped years ("experience with/using X") don't set
        # the floor when a general phrase exists (a real posting's
        # "3+ years of experience using AWS" hid its "5+ years" bar).
        ("5+ years of experience developing applications in Typescript. "
         "5+ years of experience with React and NodeJS. "
         "3+ years of experience using AWS / cloud technologies", 5),
        ("4+ years of professional experience. "
         "2+ years of experience working with Kafka", 4),
        ("6+ years of software experience and 2+ years of experience using Docker", 6),
        ("5+ years of experience. 1.5+ years of experience with Kubernetes", 5),
        # ...but when every phrase is technology-scoped, lowest still wins.
        ("3+ years of experience using AWS and 5+ years of experience with React", 3),
        # Decimal bars read whole, never from their fractional digit
        # (a real posting's "1.5+" scored as 5+).
        ("You have a total of 1.5+ years of experience as a software engineer", 1.5),
        ("1.5 to 3 years of experience", 1.5),
        ("0.5+ years of experience", 0.5),
        ("2.5+ years React, 6 years of backend experience", 2.5),
    ],
)
def test_required_experience_years(text, years):
    assert required_experience_years(text) == years


@pytest.mark.parametrize(
    "text, years",
    [
        ("6+ years building and shipping web products", 6),
        ("3+ years building production software at scale", 3),
        ("5+ years of building reliable systems", 5),
        ("Minimum 5 years of software development", 5),
        ("minimum of 4 years building web apps", 4),
        ("at least 3 years in software", 3),
        ("2-4 years of professional software engineering", 2),
        ("1–2+ years of professional software engineering", 1),
        ("5+ years of hands-on software development", 5),
        ("4+ years as a Software Engineer", 4),
        ("6+ years working as a backend engineer", 6),
        ("3+ years in a full-stack or frontend role", 3),
        ("You have 1.5+ years writing production code", 1.5),
    ],
)
def test_years_read_without_the_word_experience(text, years):
    assert required_experience_years(text) == years


@pytest.mark.parametrize(
    "text",
    [
        "You are at least 18 years of age",
        "Paid sabbatical after 5 years of employment",
        "Sabbatical for employees with 5+ years of service",
        "For over 30 years we've been making games",
        "You have 10 years to exercise your options",
        "Before founding the company she spent 18 years at a search engine",
        "Named a best workplace for 7+ years and recognized by the press",
        "Founded 10 years ago building software",
        "We have spent 10 years building the market leader",
        "5+ years in sales",
        "3+ years in marketing analytics",
    ],
)
def test_years_about_something_else_are_not_requirements(text):
    assert required_experience_years(text) is None


@pytest.mark.parametrize(
    "text, years",
    [
        ("5+ years building production software, with at least 1-2 years building LLM apps", 5),
        ("8+ years in engineering with at least 3 years in an engineering leadership role", 8),
        ("10+ years in software development with a minimum of 5+ years in engineering management", 10),
        ("6+ years of software development, including 2+ years building APIs", 6),
    ],
)
def test_sub_requirements_do_not_set_the_bar(text, years):
    assert required_experience_years(text) == years


@pytest.mark.parametrize(
    "text, years",
    [
        ("A degree or equivalent practical experience, with 5+ years building software", 5),
        ("We want someone with 3+ years in software", 3),
        ("Someone with at least 4+ years writing production code", 4),
    ],
)
def test_with_can_introduce_the_main_bar(text, years):
    assert required_experience_years(text) == years


def test_a_lone_sub_requirement_reads_as_nothing():
    assert required_experience_years("10+ years in product, with 1+ years shipping models") is None


def test_alternative_bars_still_take_the_lowest():
    text = "3+ years working as a backend engineer here OR 5+ years working as a backend engineer"
    assert required_experience_years(text) == 3


def test_the_fallback_never_overrides_a_stated_bar():
    assert required_experience_years(
        "5+ years of experience required. 2+ years building APIs"
    ) == 5


def test_required_experience_years_integral_results_are_ints():
    result = required_experience_years("5+ years of experience")
    assert isinstance(result, int)  # stored reasons should read "5", not "5.0"
