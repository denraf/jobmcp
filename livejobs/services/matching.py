"""Which jobs match a user's preferences, excluding anything they already have
a ledger row for.

Matching is two-stage: SQL narrows by cheap substring filters, then Python
applies
whole-word rules (so an exclude of 'intern' does not remove 'International').
A trailing '*' on a term makes it a prefix: 'engineer*' matches 'engineering'.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from ..models import strip_data_uris
from ..orm import Application, Board, Job, Preferences, utcnow
from .labels import label_filters

SCAN_BATCH = 500


def term_regex(term: str) -> re.Pattern:
    star = term.endswith("*")
    core = re.escape(term.rstrip("*").strip())
    return re.compile(
        rf"(?<!\w){core}" + (r"\w*" if star else r"(?!\w)"), re.I
    )


def _any(terms: list[str], text: str) -> bool:
    return any(term_regex(t).search(text) for t in terms)


def refine(job: Job, p: Preferences) -> bool:
    if not _any(p.title_include or [], job.title):
        return False
    if p.title_exclude and _any(p.title_exclude, job.title):
        return False
    if p.description_exclude and _any(
        p.description_exclude, strip_data_uris(job.description)
    ):
        return False
    comp = (job.company or "").lower()
    if p.companies_exclude and any(
        c.lower() in comp for c in p.companies_exclude
    ):
        return False
    return True


def _sql_filters(p: Preferences, user_id: int):
    f = [
        Job.active == 1,
        Board.status == "enabled",
        ~exists().where(
            Application.user_id == user_id, Application.job_id == Job.id
        ),
    ]
    f.append(
        or_(
            *[
                Job.title.contains(t.rstrip("*"), autoescape=True)
                for t in p.title_include
            ]
        )
    )
    if p.locations:
        f.append(
            or_(
                *[
                    Job.location.contains(x, autoescape=True)
                    for x in p.locations
                ]
            )
        )
    f.extend(
        label_filters(
            "remote" if p.remote_only else None,
            p.seniority or [],
            p.countries or [],
            bool(p.exclude_agencies),
        )
    )
    if p.vendors:
        f.append(Job.vendor.in_(p.vendors))
    if p.companies_include:
        f.append(
            or_(
                *[
                    Job.company.contains(c, autoescape=True)
                    for c in p.companies_include
                ]
            )
        )
    if p.max_age_days:
        cutoff = utcnow() - timedelta(days=p.max_age_days)
        f.append(
            or_(
                Job.posted_at >= cutoff,
                (Job.posted_at.is_(None)) & (Job.first_seen >= cutoff),
            )
        )
    return f


def find_matches(
    s: Session,
    user_id: int,
    p: Preferences,
    limit: int = 25,
    after_id: int | None = None,
) -> tuple[list[Job], int | None]:
    """Newest-first by job id. Returns (jobs, next_cursor); pass next_cursor as
    after_id for the next page."""
    if not p.title_include:
        return (
            [],
            None,
            # no title terms = no opinion on what to apply to; never match
            # everything
        )
    out: list[Job] = []
    cursor = after_id
    while len(out) < limit:
        q = (
            select(Job)
            .join(Board, Board.id == Job.board_id)
            .where(*_sql_filters(p, user_id))
            .order_by(Job.id.desc())
            .limit(SCAN_BATCH)
        )
        if cursor is not None:
            q = q.where(Job.id < cursor)
        batch = list(s.scalars(q))
        if not batch:
            return out, None
        for j in batch:
            cursor = j.id
            if refine(j, p):
                out.append(j)
                if len(out) == limit:
                    return out, cursor
        if len(batch) < SCAN_BATCH:
            return out, None
    return out, cursor
