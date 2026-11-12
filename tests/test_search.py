import pytest
from sqlalchemy import select

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
