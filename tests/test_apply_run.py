"""Tests for the batch application-run staging CLI helpers."""

import pytest

import apply_run
import db


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    connection.execute(
        "INSERT INTO companies (id, name, ats_type, slug) VALUES (1, 'Acme', 'greenhouse', 'acme')"
    )
    for i, score in ((1, 50), (2, 40), (3, 30)):
        connection.execute(
            """INSERT INTO jobs (id, company_id, external_id, title, url, score,
                                 gate_failed, dismissed, description)
               VALUES (?, 1, ?, ?, ?, ?, 0, 0, 'JD text')""",
            (i, f"j{i}", f"Engineer {i}", f"https://acme.example/{i}", score),
        )
    connection.commit()
    yield connection
    connection.close()


def test_top_untracked_orders_by_score_and_skips_tracked(conn):
    db.create_application(conn, 1)  # tracked: must not be listed
    rows = apply_run.top_untracked(conn, 5)
    assert [r["id"] for r in rows] == [2, 3]
    assert apply_run.top_untracked(conn, 1)[0]["id"] == 2


def test_applications_dir_prefers_profile_home(tmp_path):
    assert apply_run.applications_dir(
        {"workflow": {"home": str(tmp_path)}}
    ) == tmp_path / "Applications"
    fallback = apply_run.applications_dir({})
    assert fallback.name == "Applications"
    assert fallback.parent.name == "ResumesAndJobSearch"


def test_start_job_tracks_and_writes_role_folder(conn, tmp_path):
    info = apply_run.start_job(conn, 2, root=tmp_path)
    assert info["url"] == "https://acme.example/2"
    assert info["folder"] == tmp_path / "Acme" / "Engineer 2"
    assert info["basename"] == "Joshua_Walulik_Resume_Acme_v1"
    job_md = (info["folder"] / f"job_{info['app_id']}.md").read_text()
    assert "apply here: https://acme.example/2" in job_md
    assert "JD text" in job_md
    # The job is now tracked in considering.
    app = conn.execute(
        "SELECT * FROM applications WHERE id = ?", (info["app_id"],)
    ).fetchone()
    assert app["lane"] == "considering"

    # Re-running reuses the application instead of duplicating.
    again = apply_run.start_job(conn, 2, root=tmp_path)
    assert again["app_id"] == info["app_id"]
    assert conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"] == 1

    with pytest.raises(ValueError, match="no job"):
        apply_run.start_job(conn, 999, root=tmp_path)


def test_mark_applied_moves_lane_and_reports_missing(conn, tmp_path):
    info_a = apply_run.start_job(conn, 2, root=tmp_path)
    moved = apply_run.mark_applied(conn, [info_a["app_id"], 999])
    assert moved == [info_a["app_id"]]
    row = conn.execute(
        "SELECT lane, applied_at FROM applications WHERE id = ?", (info_a["app_id"],)
    ).fetchone()
    assert row["lane"] == "applied"
    assert row["applied_at"] is not None


def test_mark_ready_moves_lane(conn, tmp_path):
    info = apply_run.start_job(conn, 2, root=tmp_path)
    assert apply_run.mark_ready(conn, [info["app_id"], 999]) == [info["app_id"]]
    row = conn.execute(
        "SELECT lane, applied_at FROM applications WHERE id = ?", (info["app_id"],)
    ).fetchone()
    assert row["lane"] == "ready"
    assert row["applied_at"] is None  # only the applied lane stamps it


def test_resume_build_mark_ready_from_yaml_stem(conn, tmp_path):
    import resume_build
    info = apply_run.start_job(conn, 2, root=tmp_path)
    app_id = info["app_id"]
    resume_build.mark_ready(tmp_path / f"resume_{app_id}.yaml", conn)
    assert conn.execute(
        "SELECT lane FROM applications WHERE id = ?", (app_id,)
    ).fetchone()["lane"] == "ready"
    # A re-render never demotes a card that has moved on.
    db.set_application_lane(conn, app_id, "applied")
    resume_build.mark_ready(tmp_path / f"resume_{app_id}.yaml", conn)
    assert conn.execute(
        "SELECT lane FROM applications WHERE id = ?", (app_id,)
    ).fetchone()["lane"] == "applied"
    # Ad-hoc yaml names without an app id are left alone (no crash).
    resume_build.mark_ready(tmp_path / "resume_globex_custom.yaml", conn)


def test_mark_skipped_moves_lane(conn, tmp_path):
    info = apply_run.start_job(conn, 3, root=tmp_path)
    assert apply_run.mark_skipped(conn, [info["app_id"], 999]) == [info["app_id"]]
    row = conn.execute(
        "SELECT lane FROM applications WHERE id = ?", (info["app_id"],)
    ).fetchone()
    assert row["lane"] == "skipped"
    # Skipped cards can be untracked straight back to plain matches.
    assert db.untrack_application(conn, info["app_id"]) is True


def test_find_company_dir_reuses_name_variants(tmp_path):
    (tmp_path / "globex").mkdir()
    (tmp_path / "hooli").mkdir()
    assert apply_run.find_company_dir(tmp_path, "Globex Corp") == tmp_path / "globex"
    # No normalized containment match -> new folder named after company.
    assert apply_run.find_company_dir(tmp_path, "Initech") == tmp_path / "Initech"
    # _archive and hidden folders are never matched.
    (tmp_path / "_archive").mkdir()
    assert apply_run.find_company_dir(tmp_path, "Archive Co") == tmp_path / "Archive Co"


def test_next_resume_version_scans_company_tree(tmp_path):
    company = tmp_path / "acme"
    (company / "old role").mkdir(parents=True)
    (company / "old role" / "Joshua_Walulik_Resume_Acme_v3.docx").write_text("x")
    (company / "Joshua_Walulik_Resume_Acme_v1.pdf").write_text("x")
    assert apply_run.next_resume_version(company) == 4
    assert apply_run.next_resume_version(tmp_path / "missing") == 1


def test_start_job_scores_unscored_job(conn, tmp_path, monkeypatch):
    # Gate-failed jobs arrive unscored; start must score them like the
    # app's track route so the staged job file carries a breakdown.
    conn.execute(
        """INSERT INTO jobs (id, company_id, external_id, title, url, score,
                             gate_failed, dismissed, description)
           VALUES (9, 1, 'j9', 'Software Engineer', 'https://acme.example/9',
                   NULL, 1, 0, 'Build products with react and typescript')"""
    )
    conn.commit()
    import scrapers.sweep

    monkeypatch.setattr(
        scrapers.sweep,
        "load_profile",
        lambda *a, **k: {
            "companies": [{"ats": "greenhouse", "slug": "acme", "weight": 1.5}],
            "scoring": {
                "target_titles": ["Software Engineer"],
                "title_match_weight": 30,
                "exact_title_bonus": 15,
                "required_keywords": ["react", "typescript"],
                "required_keyword_weight": 5,
                "preferred_keywords": [],
                "preferred_keyword_weight": 2,
                "negative_keywords": [],
                "negative_keyword_weight": -10,
                "must_haves": [],
                "missing_must_have_penalty": -20,
                "seniority": {"target": "mid", "multipliers": {"mid": 1.0}},
                "sparse_description": {"min_chars": 1, "multiplier": 0.85},
            },
        },
    )
    info = apply_run.start_job(conn, 9, root=tmp_path)
    row = conn.execute("SELECT score, score_breakdown FROM jobs WHERE id = 9").fetchone()
    assert row["score"] is not None and row["score"] > 0
    assert "react" in row["score_breakdown"]
    job_md = (info["folder"] / f"job_{info['app_id']}.md").read_text()
    assert f"- score: {row['score']}" in job_md
    assert "required keyword" in job_md

    # Already-scored jobs are left untouched (score 40 from the fixture).
    apply_run.start_job(conn, 2, root=tmp_path)
    assert conn.execute("SELECT score FROM jobs WHERE id = 2").fetchone()["score"] == 40
