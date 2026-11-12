"""Idempotent schema upgrades. `create_all` makes new tables but never alters
existing ones, so
columns added after a release are applied here. Run by `livejobs init-db` (safe
to run every deploy)."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from . import db
from .orm import Base

log = logging.getLogger("livejobs.migrations")


def is_current() -> bool:
    """True once every table and every column added by an upgrade exists. Lets
    services that don't run migrations
    (the scheduler) wait for the web service to finish them instead of racing
    it."""
    insp = inspect(db.engine())
    if not all(insp.has_table(t) for t in Base.metadata.tables):
        return False
    return (
        "email_verified_at" in {c["name"] for c in insp.get_columns("users")}
        and "source" in {c["name"] for c in insp.get_columns("boards")}
        and {"pages_total", "trigger"}
        <= {c["name"] for c in insp.get_columns("inventory_runs")}
        and {"workplace", "seniority", "country"}
        <= {c["name"] for c in insp.get_columns("jobs")}
        and "is_agency" in {c["name"] for c in insp.get_columns("boards")}
        and "sort_at" in {c["name"] for c in insp.get_columns("jobs")}
        and {"seniority", "countries", "exclude_agencies"}
        <= {c["name"] for c in insp.get_columns("preferences")}
    )


def ensure_schema() -> list[str]:
    """Create missing tables and apply column upgrades. Returns what it
    changed."""
    changed: list[str] = []
    eng = db.engine()
    Base.metadata.create_all(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("users")}
    if "email_verified_at" not in cols:
        with eng.begin() as c:
            c.execute(
                text(
                    "ALTER TABLE users ADD COLUMN email_verified_at DATETIME "
                    "NULL"
                )
            )
            # accounts that existed before verification was introduced are
            # grandfathered in
            c.execute(
                text(
                    "UPDATE users SET email_verified_at = created_at WHERE "
                    "email_verified_at IS NULL"
                )
            )
        changed.append(
            "users.email_verified_at (existing users marked verified)"
        )
    bcols = {c["name"] for c in inspect(eng).get_columns("boards")}
    if "source" not in bcols:
        with eng.begin() as c:
            c.execute(
                text(
                    "ALTER TABLE boards ADD COLUMN source VARCHAR(16) NOT "
                    "NULL DEFAULT 'crawl'"
                )
            )
            if "added_by" not in bcols:
                c.execute(
                    text("ALTER TABLE boards ADD COLUMN added_by INT NULL")
                )
            # Before this column existed, boards added by hand were the ones
            # Common Crawl never counted (hits = 0).
            c.execute(
                text("UPDATE boards SET source = 'manual' WHERE hits = 0")
            )
        changed.append(
            "boards.source, boards.added_by (boards with no Common Crawl hits "
            "marked manual)"
        )
    icols = {c["name"] for c in inspect(eng).get_columns("inventory_runs")}
    if not {"pages_total", "trigger"} <= icols:
        with eng.begin() as c:
            if "pages_total" not in icols:
                c.execute(
                    text(
                        "ALTER TABLE inventory_runs ADD COLUMN pages_total "
                        "INT NOT NULL DEFAULT 0"
                    )
                )
            if "trigger" not in icols:
                c.execute(
                    text(
                        "ALTER TABLE inventory_runs ADD COLUMN `trigger` "
                        "VARCHAR(16) NOT NULL DEFAULT 'scheduled'"
                    )
                )
        changed.append("inventory_runs.pages_total, inventory_runs.trigger")
    changed += _classification_columns(eng)
    changed += _sort_column(eng)
    changed += _filter_indexes(eng)
    for name in changed:
        log.info("applied: %s", name)
    return changed


BACKFILL_BATCH = 2000


def _sort_column(eng) -> list[str]:
    """jobs.sort_at, the date lists sort by, and its index. Filled in one
    statement: it is a plain column-to-column update."""
    if "sort_at" in {c["name"] for c in inspect(eng).get_columns("jobs")}:
        return []
    with eng.begin() as c:
        c.execute(
            text(
                "ALTER TABLE jobs ADD COLUMN sort_at DATETIME NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            )
        )
        c.execute(
            text(
                "UPDATE jobs SET sort_at = "
                "LEAST(COALESCE(posted_at, first_seen), UTC_TIMESTAMP())"
            )
        )
        c.execute(
            text(
                "CREATE INDEX ix_jobs_active_sort "
                "ON jobs (active, sort_at, id)"
            )
        )
    return ["jobs.sort_at (lists now sort by publish date)"]


FILTER_INDEXES = {
    "ix_jobs_active_workplace": "active, workplace, board_id",
    "ix_jobs_active_seniority": "active, seniority, board_id",
    "ix_jobs_active_country": "active, country, board_id",
    "ix_jobs_browse": (
        "active, sort_at, id, board_id, workplace, seniority, country"
    ),
}


def _filter_indexes(eng) -> list[str]:
    """Indexes for the jobs-page filters. Without them a filtered count reads
    every posting's full row, descriptions included: tens of seconds on a
    large table."""
    have = {i["name"] for i in inspect(eng).get_indexes("jobs")}
    made = []
    for name, cols in FILTER_INDEXES.items():
        if name not in have:
            with eng.begin() as c:
                c.execute(text(f"CREATE INDEX {name} ON jobs ({cols})"))
            made.append(name)
    return [f"index {n}" for n in made]


def _classification_columns(eng) -> list[str]:
    """Labels added after the first release: jobs get workplace / seniority /
    country, boards get an agency flag, preferences get filters for them.
    Existing rows are labelled once, in batches (so memory stays flat)."""
    from . import classify

    changed: list[str] = []
    jcols = {c["name"] for c in inspect(eng).get_columns("jobs")}
    if not {"workplace", "seniority", "country"} <= jcols:
        with eng.begin() as c:
            for col, ddl in (
                ("workplace", "VARCHAR(8) NOT NULL DEFAULT ''"),
                ("seniority", "VARCHAR(12) NOT NULL DEFAULT ''"),
                ("country", "VARCHAR(2) NOT NULL DEFAULT ''"),
            ):
                if col not in jcols:
                    c.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}"))
        n = backfill_job_labels(eng)
        changed.append(f"jobs.workplace/seniority/country ({n} rows labelled)")
    bcols = {c["name"] for c in inspect(eng).get_columns("boards")}
    if "is_agency" not in bcols:
        with eng.begin() as c:
            c.execute(
                text(
                    "ALTER TABLE boards ADD COLUMN is_agency BOOLEAN "
                    "NOT NULL DEFAULT 0"
                )
            )
            if "agency_override" not in bcols:
                c.execute(
                    text(
                        "ALTER TABLE boards ADD COLUMN agency_override "
                        "BOOLEAN NULL"
                    )
                )
            rows = c.execute(
                text("SELECT id, board, company_name FROM boards")
            ).all()
            flagged = [
                r.id
                for r in rows
                if classify.is_agency(r.company_name, r.board)
            ]
            for i in range(0, len(flagged), 500):
                marks = ",".join(str(int(x)) for x in flagged[i : i + 500])
                c.execute(
                    text(
                        "UPDATE boards SET is_agency = 1 "
                        f"WHERE id IN ({marks})"
                    )
                )
        changed.append("boards.is_agency (existing boards flagged)")
    pcols = {c["name"] for c in inspect(eng).get_columns("preferences")}
    if not {"seniority", "countries", "exclude_agencies"} <= pcols:
        with eng.begin() as c:
            if "seniority" not in pcols:
                c.execute(
                    text(
                        "ALTER TABLE preferences "
                        "ADD COLUMN seniority JSON NULL"
                    )
                )
            if "countries" not in pcols:
                c.execute(
                    text(
                        "ALTER TABLE preferences "
                        "ADD COLUMN countries JSON NULL"
                    )
                )
            if "exclude_agencies" not in pcols:
                c.execute(
                    text(
                        "ALTER TABLE preferences ADD COLUMN exclude_agencies "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
        changed.append("preferences.seniority/countries/exclude_agencies")
    return changed


def backfill_job_labels(eng, batch: int = BACKFILL_BATCH) -> int:
    """Label every job, `batch` rows at a time. Returns how many."""
    from . import classify

    done, last = 0, 0
    while True:
        with eng.begin() as c:
            rows = c.execute(
                text(
                    "SELECT id, title, location FROM jobs WHERE id > :last "
                    "ORDER BY id LIMIT :n"
                ),
                {"last": last, "n": batch},
            ).all()
            if not rows:
                return done
            params = [
                {
                    "id": r.id,
                    "w": classify.workplace(r.location, r.title),
                    "s": classify.seniority(r.title),
                    "c": classify.country(r.location),
                }
                for r in rows
            ]
            c.execute(
                text(
                    "UPDATE jobs SET workplace = :w, seniority = :s, "
                    "country = :c WHERE id = :id"
                ),
                params,
            )
        last = rows[-1].id
        done += len(rows)
