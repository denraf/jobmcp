"""Idempotent schema upgrades. `create_all` makes new tables but never alters
existing ones, so
columns added after a release are applied here. Run by `jobmcp init-db` (safe
to run every deploy)."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from . import db
from .orm import Base

log = logging.getLogger("jobmcp.migrations")


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
        and {c for c, _ in SALARY_COLUMNS}
        <= {c["name"] for c in insp.get_columns("jobs")}
        and "visa" in {c["name"] for c in insp.get_columns("jobs")}
        and "visa" in {c["name"] for c in insp.get_columns("preferences")}
        and set(FULLTEXT_INDEXES)
        <= {i["name"] for i in insp.get_indexes("jobs")}
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
    changed += _salary_columns(eng)
    changed += _visa_column(eng)
    changed += _filter_indexes(eng)
    changed += _fulltext_indexes(eng)
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


SALARY_COLUMNS = (
    ("salary_min", "INT NULL"),
    ("salary_max", "INT NULL"),
    ("salary_currency", "VARCHAR(3) NOT NULL DEFAULT ''"),
    ("salary_period", "VARCHAR(5) NOT NULL DEFAULT ''"),
    ("salary_usd_min", "INT NULL"),
    ("salary_usd_max", "INT NULL"),
)


def _salary_columns(eng) -> list[str]:
    """The pay found in each posting. Existing postings are read once, in
    batches; syncs also refresh it, so an interrupted backfill is finished by
    the next hourly run."""
    have = {c["name"] for c in inspect(eng).get_columns("jobs")}
    missing = [(c, ddl) for c, ddl in SALARY_COLUMNS if c not in have]
    if not missing:
        return []
    with eng.begin() as c:
        for col, ddl in missing:
            c.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}"))
    n = backfill_job_salaries(eng)
    return [f"jobs.salary_* ({n} postings carry a stated salary)"]


def backfill_job_salaries(eng, batch: int = 500) -> int:
    """Read the pay out of every posting's text, `batch` rows at a time.
    Returns how many postings state one."""
    from . import salary
    from .models import strip_data_uris

    found, last = 0, 0
    while True:
        with eng.begin() as c:
            rows = c.execute(
                text(
                    "SELECT id, title, description FROM jobs WHERE id > :last "
                    "ORDER BY id LIMIT :n"
                ),
                {"last": last, "n": batch},
            ).all()
            if not rows:
                return found
            params = []
            for r in rows:
                s = salary.parse(strip_data_uris(r.description or ""))
                if not s:
                    continue
                lo, hi = salary.usd_range(s)
                params.append(
                    {
                        "id": r.id,
                        "lo": s.low,
                        "hi": s.high,
                        "cur": s.currency,
                        "per": s.period,
                        "ulo": lo,
                        "uhi": hi,
                    }
                )
            if params:
                c.execute(
                    text(
                        "UPDATE jobs SET salary_min = :lo, salary_max = :hi, "
                        "salary_currency = :cur, salary_period = :per, "
                        "salary_usd_min = :ulo, salary_usd_max = :uhi "
                        "WHERE id = :id"
                    ),
                    params,
                )
                found += len(params)
        last = rows[-1].id


def _visa_column(eng) -> list[str]:
    """jobs.visa, read from each description, and preferences.visa. Existing
    postings are read once, in batches; syncs refresh it too."""
    changed = []
    if "visa" not in {
        c["name"] for c in inspect(eng).get_columns("preferences")
    }:
        with eng.begin() as c:
            c.execute(
                text(
                    "ALTER TABLE preferences ADD COLUMN visa VARCHAR(12) "
                    "NOT NULL DEFAULT ''"
                )
            )
        changed.append("preferences.visa")
    if "visa" in {c["name"] for c in inspect(eng).get_columns("jobs")}:
        return changed
    with eng.begin() as c:
        c.execute(
            text(
                "ALTER TABLE jobs ADD COLUMN visa VARCHAR(3) "
                "NOT NULL DEFAULT ''"
            )
        )
    n = backfill_job_visa(eng)
    return changed + [f"jobs.visa ({n} postings state a visa position)"]


def backfill_job_visa(eng, batch: int = 500) -> int:
    """Read the visa position out of every posting's text, `batch` rows at a
    time. Returns how many postings state one."""
    from . import classify
    from .models import strip_data_uris

    found, last = 0, 0
    while True:
        with eng.begin() as c:
            rows = c.execute(
                text(
                    "SELECT id, description FROM jobs WHERE id > :last "
                    "ORDER BY id LIMIT :n"
                ),
                {"last": last, "n": batch},
            ).all()
            if not rows:
                return found
            params = [
                {"id": r.id, "v": v}
                for r in rows
                if (v := classify.visa(strip_data_uris(r.description or "")))
            ]
            if params:
                c.execute(
                    text("UPDATE jobs SET visa = :v WHERE id = :id"), params
                )
                found += len(params)
        last = rows[-1].id


FILTER_INDEXES = {
    "ix_jobs_active_visa": "active, visa, board_id",
    "ix_jobs_active_workplace": "active, workplace, board_id",
    "ix_jobs_active_seniority": "active, seniority, board_id",
    "ix_jobs_active_country": "active, country, board_id",
    "ix_jobs_active_salary": (
        "active, salary_usd_min, salary_usd_max, board_id"
    ),
    "ix_jobs_browse": (
        "active, sort_at, id, board_id, workplace, seniority, country, "
        "salary_usd_min, salary_usd_max"
    ),
}


def _filter_indexes(eng) -> list[str]:
    """Indexes for the jobs-page filters. Without them a filtered count reads
    every posting's full row, descriptions included: tens of seconds on a
    large table."""
    have = {
        i["name"]: list(i["column_names"])
        for i in inspect(eng).get_indexes("jobs")
    }
    made = []
    for name, cols in FILTER_INDEXES.items():
        want = [x.strip() for x in cols.split(",")]
        if have.get(name) == want:
            continue
        with eng.begin() as c:
            if name in have:  # an older definition: it lacks a column
                c.execute(text(f"DROP INDEX {name} ON jobs"))
            c.execute(text(f"CREATE INDEX {name} ON jobs ({cols})"))
        made.append(name)
    return [f"index {n}" for n in made]


FULLTEXT_INDEXES = {
    "ft_jobs_title": "title, company",
    "ft_jobs_company": "company",
    "ft_jobs_text": "title, company, description",
}


def _fulltext_indexes(eng) -> list[str]:
    """The word-search indexes. Building one reads every posting, so on a big
    table this takes minutes (the description one the longest); it runs once.
    """
    have = {i["name"] for i in inspect(eng).get_indexes("jobs")}
    made = []
    for name, cols in FULLTEXT_INDEXES.items():
        if name in have:
            continue
        with eng.begin() as c:
            c.execute(text(f"CREATE FULLTEXT INDEX {name} ON jobs ({cols})"))
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
