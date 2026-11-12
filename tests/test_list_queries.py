"""What the jobs list sends to MySQL. These pin the causes of a 9-second page
on 300,000 postings: a condition that made MySQL ignore its index, rows read
whole (descriptions included), and one query per row."""

from sqlalchemy import event

from jobmcp import db, migrations
from jobmcp.services import search, sync

from .helpers import fj, mk_board


def statements(run):
    seen = []

    @event.listens_for(db.engine(), "before_cursor_execute")
    def grab(conn, cur, st, params, ctx, many):
        seen.append(" ".join(st.split()))

    try:
        run()
    finally:
        event.remove(db.engine(), "before_cursor_execute", grab)
    return seen


def test_the_list_uses_few_plain_queries(fresh_db):
    sync.sync_board(
        mk_board(),
        lambda v, s: [fj(f"Job {i}", desc="x" * 500) for i in range(30)],
    )

    def run():
        with db.session() as s:
            r = search.search_jobs(
                s, None, browse=True, include_applied=True, limit=25
            )
            [h.job.board.is_agency for h in r["hits"]]  # what job_out reads
            assert len(r["hits"]) == 25 and r["total"] == 30

    sql = statements(run)
    assert len(sql) == 3, sql  # count, the page's ids, the rows: not 25 more
    assert not any("IS true" in q or "IS 1" in q for q in sql), sql
    count, ids, rows = sql
    assert count.startswith(
        "SELECT count(jobs.id)"
    )  # never a wrapped subquery
    assert "description" not in ids and "description" not in count
    assert "jobs.description" not in rows  # the list never shows one
    assert "JOIN boards" in rows  # the board comes with each row


def test_pages_and_order_are_unchanged(fresh_db):
    sync.sync_board(
        mk_board(), lambda v, s: [fj(f"Job {i:02d}") for i in range(12)]
    )
    with db.session() as s:
        a = search.search_jobs(
            s, None, browse=True, include_applied=True, limit=5
        )
        b = search.search_jobs(
            s, None, browse=True, include_applied=True, limit=5, offset=5
        )
    ids = [h.job.id for h in a["hits"]] + [h.job.id for h in b["hits"]]
    assert len(set(ids)) == 10 and a["next_offset"] == 5 and a["total"] == 12


def test_the_migration_adds_the_list_indexes(fresh_db):
    names = set(migrations.FILTER_INDEXES)
    assert "ix_jobs_browse" in names
    with db.engine().begin() as c:
        for n in names:
            c.exec_driver_sql(f"DROP INDEX {n} ON jobs")
    changed = migrations.ensure_schema()
    assert {f"index {n}" for n in names} <= set(changed)
    assert migrations.ensure_schema() == []  # and then it is idempotent
