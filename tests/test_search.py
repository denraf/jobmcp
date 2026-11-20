import pytest
from sqlalchemy import select, text

from jobmcp import db
from jobmcp.orm import Job
from jobmcp.services import ServiceError, applications, auth, search, sync

from .helpers import fj, mk_board, mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture()
def uid():
    u = mk_user()
    b1, b2 = mk_board("greenhouse", "acme"), mk_board("lever", "beta")
    sync.sync_board(
        b1,
        lambda v, s: [
            fj(
                "Senior Data Engineer",
                desc="We use Python, Airflow and dbt. Work with Snowflake "
                "daily.",
                loc="Remote - US",
            ),
            fj(
                "Backend Engineer",
                desc="Golang services. Our parent is Google Cloud partner.",
                loc="Berlin",
            ),
            fj(
                "Machine Learning Engineer",
                desc="Train models on GPUs; strong machine learning "
                "background.",
                loc="Remote",
            ),
            fj(
                "Data Engineering Intern",
                desc="Learn Python and SQL.",
                loc="Remote",
            ),
            fj(
                "Office Manager",
                desc="Keep the office running. Some python scripting a plus.",
                loc="Paris",
            ),
        ],
    )
    sync.sync_board(
        b2,
        lambda v, s: [
            fj(
                "Staff Engineer",
                board="beta",
                company="Beta",
                vendor="lever",
                desc="Go and Kubernetes.",
                loc="Remote - EU",
            )
        ],
    )
    return u


def S(uid, **kw):
    with db.session() as s:
        r = search.search_jobs(s, uid, **kw)
        return [h.job.title for h in r["hits"]], r


def test_requires_a_query(uid):
    with db.session() as s, pytest.raises(ServiceError):
        search.search_jobs(s, uid)
    with db.session() as s, pytest.raises(ServiceError):
        search.search_jobs(s, uid, title="   ")


def test_title_search(uid):
    assert set(S(uid, title="data engineer")[0]) == {"Senior Data Engineer"}
    assert set(S(uid, title="engineer*")[0]) >= {
        "Senior Data Engineer",
        "Data Engineering Intern",
        "Backend Engineer",
    }
    assert S(uid, title="DATA ENGINEER")[0] == [
        "Senior Data Engineer"
    ]  # case-insensitive
    assert set(S(uid, title="engineer -intern -senior")[0]) == {
        "Machine Learning Engineer",
        "Backend Engineer",
        "Staff Engineer",
    }
    got = S(uid, title="engineer* -intern")[0]
    assert (
        "Data Engineering Intern" not in got and "Senior Data Engineer" in got
    )


def test_keyword_in_description_and_ranking(uid):
    titles, r = S(uid, keywords="python")
    assert set(titles) == {
        "Senior Data Engineer",
        "Data Engineering Intern",
        "Office Manager",
    }
    assert all(not h.in_title for h in r["hits"])
    assert (
        "python" in r["hits"][0].snippet.lower()
    )  # snippet shows the matched context
    # title hits rank above description-only hits
    titles, r = S(uid, keywords="machine")
    assert titles[0] == "Machine Learning Engineer" and r["hits"][0].in_title


def test_whole_word_phrase_and_modes(uid):
    assert S(uid, keywords="go")[0] == [
        "Staff Engineer"
    ]  # not Golang, not Google
    assert S(uid, keywords='"machine learning"')[0] == [
        "Machine Learning Engineer"
    ]
    assert set(S(uid, keywords="python airflow")[0]) == {
        "Senior Data Engineer"
    }  # all (default)
    assert set(S(uid, keywords="airflow kubernetes", match="any")[0]) == {
        "Senior Data Engineer",
        "Staff Engineer",
    }
    assert S(uid, keywords="python -intern -manager")[0] == [
        "Senior Data Engineer"
    ]
    assert (
        S(uid, keywords='"unbalanced')[0] == []
    )  # bad quoting degrades, doesn't crash
    with db.session() as s, pytest.raises(ServiceError):
        search.search_jobs(s, uid, keywords="x", match="some")


def test_title_and_keywords_combine_and_filters(uid):
    assert set(S(uid, title="engineer*", keywords="python")[0]) == {
        "Senior Data Engineer",
        "Data Engineering Intern",
    }
    assert set(S(uid, keywords="python", remote_only=True)[0]) == {
        "Senior Data Engineer",
        "Data Engineering Intern",
    }
    assert S(uid, keywords="python", location="paris")[0] == ["Office Manager"]
    assert S(uid, title="engineer*", vendor="lever")[0] == ["Staff Engineer"]
    assert S(uid, title="engineer*", company="beta")[0] == ["Staff Engineer"]
    with db.session() as s, pytest.raises(ServiceError):
        search.search_jobs(s, uid, title="x", vendor="nope")


def test_wildcards_are_literal(uid):
    # % and _ in user input must not act as SQL LIKE wildcards
    assert S(uid, keywords="100%")[0] == []
    assert S(uid, title="e_gineer")[0] == []


def test_applied_hidden_unless_requested_and_status_shown(uid):
    with db.session() as s:
        jid = s.scalar(select(Job.id).where(Job.title == "Office Manager"))
        applications.record(s, s.get(auth.User, uid), jid, "applied")
    assert "Office Manager" not in S(uid, keywords="python")[0]
    titles, r = S(uid, keywords="python", include_applied=True)
    assert "Office Manager" in titles
    assert {h.job.title: h.status for h in r["hits"]}[
        "Office Manager"
    ] == "applied"
    other = mk_user("b@x.io")
    assert (
        "Office Manager" in S(other, keywords="python")[0]
    )  # someone else's ledger doesn't affect me


def test_closed_and_disabled_board_jobs_not_found(uid):
    with db.session() as s:
        from jobmcp.services import boards
        from jobmcp.orm import Board

        bid = s.scalar(select(Board.id).where(Board.board == "beta"))
        boards.update_board(s, bid, status="disabled")
    assert S(uid, keywords="kubernetes")[0] == []


def test_pagination(uid):
    titles, r = S(uid, title="engineer*", limit=2)
    assert r["total"] >= 4 and len(titles) == 2 and r["next_offset"] == 2
    t2, r2 = S(uid, title="engineer*", limit=2, offset=2)
    assert not set(titles) & set(t2)


# ── full-text narrowing ──
def _fts_seed():
    sync.sync_board(
        mk_board(),
        lambda v, s: [
            fj("Data Engineer", company="Acme Robotics", desc="airflow"),
            fj("Chef", company="Acme Robotics", desc="we love data engineer"),
            fj("Go Developer", company="Globex", desc="Go and Kubernetes"),
            fj("Designer", company="Initech", desc="figma"),
        ],
    )


def test_ft_terms_are_safe_and_skip_what_the_index_cannot_hold():
    from jobmcp.services.search import ft_query, ft_term

    assert ft_term("engineer*") == "engineer*"
    assert ft_term("machine learning") == '"machine learning"'
    assert ft_term("go") is None and ft_term("the") is None
    assert ft_term("c++") is None
    assert ft_term('x") +(evil') is None  # no operator can get through
    assert ft_query(["go", "airflow"]) == "+airflow"
    assert ft_query(["go", "airflow"], any_term=True) is None
    assert ft_query(["airflow", "spark"], any_term=True) == "airflow spark"


def test_search_by_company_and_short_terms():

    _fts_seed()
    with db.session() as s:
        r = search.search_jobs(s, None, company="acme")
        assert {h.job.title for h in r["hits"]} == {"Data Engineer", "Chef"}
        r = search.search_jobs(s, None, title="go")  # too short for the index
        assert [h.job.title for h in r["hits"]] == ["Go Developer"]
        r = search.search_jobs(s, None, keywords="go kubernetes")
        assert [h.job.title for h in r["hits"]] == ["Go Developer"]


def test_title_hits_outrank_description_hits():

    _fts_seed()
    with db.session() as s:
        r = search.search_jobs(s, None, keywords="data engineer")
        assert [h.job.title for h in r["hits"]] == ["Data Engineer", "Chef"]
        assert [h.in_title for h in r["hits"]] == [True, False]


def test_company_directory():

    _fts_seed()
    with db.session() as s:
        s.execute(text("UPDATE boards SET job_count = 4"))
        s.execute(text("UPDATE boards SET company_name = 'Acme Robotics'"))
    with db.session() as s:
        assert search.search_companies(s, "acme") == [
            {"company": "Acme Robotics", "open_jobs": 4}
        ]
        assert search.search_companies(s, "zzz") == []


def test_schema_has_the_fulltext_indexes():
    from jobmcp import migrations

    assert migrations.is_current()
    assert migrations.ensure_schema() == []  # nothing left to build


# ── visa sponsorship ──
def test_visa_is_read_from_the_description():
    from jobmcp import classify

    assert classify.visa("We offer visa sponsorship for this role.") == "yes"
    assert classify.visa("H-1B transfers welcome") == "yes"
    assert classify.visa("We do not offer visa sponsorship.") == "no"
    assert (
        classify.visa("Must be authorized to work without sponsorship") == "no"
    )
    assert classify.visa("Sponsorship is not available") == "no"
    assert classify.visa("Great team, remote first.") == ""
    assert classify.visa("") == ""


def _visa_seed():
    sync.sync_board(
        mk_board(),
        lambda v, s: [
            fj("Backend Dev", desc="Visa sponsorship is available."),
            fj("Frontend Dev", desc="We are unable to sponsor visas."),
            fj("Data Dev", desc="Join our team."),
        ],
    )


def test_visa_filter():
    _visa_seed()
    with db.session() as s:

        def titles(v):
            r = search.search_jobs(s, None, title="dev", visa=v)
            return {h.job.title for h in r["hits"]}

        assert titles("offered") == {"Backend Dev"}
        assert titles("not_excluded") == {"Backend Dev", "Data Dev"}
        assert len(titles(None)) == 3
        with pytest.raises(ServiceError):
            titles("maybe")


def test_visa_preference_and_agent_output():
    from jobmcp import mcp_server
    from jobmcp.services import matching, prefs
    from jobmcp.orm import User

    uid = mk_user()
    _visa_seed()
    with db.session() as s:
        u = s.get(User, uid)
        p = prefs.update_preferences(
            s, u, {"title_include": ["dev"], "visa": "offered"}
        )
        assert prefs.prefs_dict(p)["visa"] == "offered"
        jobs, _ = matching.find_matches(s, uid, p, 10, None)
        assert [j.title for j in jobs] == ["Backend Dev"]
        assert mcp_server._job(jobs[0])["visa_sponsorship"] == "offered"
        with pytest.raises(ServiceError):
            prefs.update_preferences(s, u, {"visa": "x"})
