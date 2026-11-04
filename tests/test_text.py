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


def test_required_experience_years_integral_results_are_ints():
    result = required_experience_years("5+ years of experience")
    assert isinstance(result, int)  # stored reasons should read "5", not "5.0"
