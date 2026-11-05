import pytest

import db
import referral_watch

YEARS = {"junior_max": 1, "senior_min": 5}
CFG = {
    "company_suffixes": ["Inc"],
    "company_aliases": [["Acme", "Acme Holdings"]],
    "do_not_contact": [{"company": "Umbrella", "email_domains": [], "reason": "former employer"}],
    "referral_watch": {"levels": ["mid"], "title_keywords": ["software", "full stack"]},
}


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


def add_company(conn, name):
    return conn.execute(
        "INSERT INTO companies (name, ats_type, slug) VALUES (?, 'greenhouse', ?)",
        (name, name.lower().replace(" ", "-")),
    ).lastrowid


def add_job(conn, company_id, title="Software Engineer",
            description="3+ years of experience", **overrides):
    fields = {"gate_failed": 0, "dismissed": 0, "external_id": title + str(overrides),
              "first_seen": "2026-03-01 00:00:00", **overrides}
    job_id = conn.execute(
        f"""
        INSERT INTO jobs (company_id, title, description, {", ".join(fields)})
        VALUES (?, ?, ?, {", ".join("?" for _ in fields)})
        """,
        (company_id, title, description, *fields.values()),
    ).lastrowid
    conn.commit()
    return job_id


def add_contact(conn, name="Pat Doe", company="Acme", **overrides):
    fields = {"track": "referral", "source": "own_pipeline", "standing_referral": 1,
              **overrides}
    return db.insert_contact(conn, name=name, company=company, **fields)


def matches(conn):
    return referral_watch.find_matches(conn, CFG, YEARS)


def test_finds_a_mid_level_role_at_the_contacts_company(conn):
    job_id = add_job(conn, add_company(conn, "Acme"))
    contact_id = add_contact(conn)
    [match] = matches(conn)
    assert match["job_id"] == job_id
    assert match["contact_id"] == contact_id
    assert match["contact_name"] == "Pat Doe"
    assert match["company"] == "Acme"
    assert "mid level" in match["reason"]


def test_works_for_any_number_of_contacts_and_companies(conn):
    acme, globex = add_company(conn, "Acme"), add_company(conn, "Globex")
    add_job(conn, acme)
    add_job(conn, globex)
    add_contact(conn, name="Pat Doe", company="Acme")
    add_contact(conn, name="Sam Roe", company="Globex")
    assert sorted((m["contact_name"], m["company"]) for m in matches(conn)) == [
        ("Pat Doe", "Acme"), ("Sam Roe", "Globex"),
    ]


def test_matches_a_company_listed_under_an_alias(conn):
    add_job(conn, add_company(conn, "Acme Holdings"))
    add_contact(conn, company="Acme")
    assert len(matches(conn)) == 1


def test_a_second_board_for_the_same_company_is_covered(conn):
    add_job(conn, add_company(conn, "Acme"))
    add_job(conn, add_company(conn, "Acme Holdings"), title="Software Developer in Software")
    add_contact(conn)
    assert len(matches(conn)) == 2


def test_other_companies_are_ignored(conn):
    add_job(conn, add_company(conn, "Globex"))
    add_contact(conn, company="Acme")
    assert matches(conn) == []


def test_no_standing_referrals_means_no_matches(conn):
    add_job(conn, add_company(conn, "Acme"))
    add_contact(conn, standing_referral=0)
    assert matches(conn) == []


@pytest.mark.parametrize(
    "overrides",
    [{"status": "Skip"}, {"status": "Cold"}, {"track": "mentor"}, {"source": "peer_list"}],
)
def test_inactive_contacts_are_not_watched(conn, overrides):
    add_job(conn, add_company(conn, "Acme"))
    add_contact(conn, **overrides)
    assert matches(conn) == []


def test_do_not_contact_is_never_watched(conn):
    add_job(conn, add_company(conn, "Umbrella"))
    add_contact(conn, company="Umbrella")
    assert matches(conn) == []


def test_removed_contacts_are_not_watched(conn):
    add_job(conn, add_company(conn, "Acme"))
    db.remove_contact(conn, add_contact(conn))
    assert matches(conn) == []


def test_gated_out_roles_are_ignored(conn):
    add_job(conn, add_company(conn, "Acme"), gate_failed=1)
    add_contact(conn)
    assert matches(conn) == []


def test_roles_not_yet_gated_are_ignored(conn):
    add_job(conn, add_company(conn, "Acme"), gate_failed=None)
    add_contact(conn)
    assert matches(conn) == []


def test_dismissing_a_role_clears_it(conn):
    job_id = add_job(conn, add_company(conn, "Acme"))
    add_contact(conn)
    db.dismiss_job(conn, job_id)
    assert matches(conn) == []


def test_tying_a_role_to_the_contact_clears_it(conn):
    job_id = add_job(conn, add_company(conn, "Acme"))
    contact_id = add_contact(conn)
    db.link_contact_application(conn, contact_id, db.create_application(conn, job_id))
    assert matches(conn) == []


def test_tracking_without_tying_keeps_the_role_listed(conn):
    job_id = add_job(conn, add_company(conn, "Acme"))
    add_contact(conn)
    db.create_application(conn, job_id)
    assert len(matches(conn)) == 1


def test_tying_to_one_contact_leaves_it_for_another(conn):
    job_id = add_job(conn, add_company(conn, "Acme"))
    first = add_contact(conn, name="Pat Doe")
    add_contact(conn, name="Sam Roe")
    db.link_contact_application(conn, first, db.create_application(conn, job_id))
    assert [m["contact_name"] for m in matches(conn)] == ["Sam Roe"]


def test_newest_roles_come_first(conn):
    acme = add_company(conn, "Acme")
    add_job(conn, acme, title="Software Engineer One", first_seen="2026-03-01 00:00:00")
    add_job(conn, acme, title="Software Engineer Two", first_seen="2026-03-05 00:00:00")
    add_contact(conn)
    assert [m["title"] for m in matches(conn)] == ["Software Engineer Two", "Software Engineer One"]
