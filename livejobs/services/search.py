"""Ad-hoc job search by title and/or any keyword in the title or description.

Query syntax (`keywords` / `title`): space-separated terms, all required;
  "machine learning"   quoted phrase
  engineer*            trailing * = prefix (matches engineering)
  -intern              exclude (whole word)
Matching is whole-word and case-insensitive, so 'go' does not match 'Google'.

Like preference matching this is two-stage: SQL substring filters narrow the
candidates (newest
first, at most CANDIDATE_CAP), then Python applies the whole-word rules and
ranks title hits above
description-only hits. `truncated` in the result says the cap was hit, i.e. add
filters or terms.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, defer, joinedload

from ..models import strip_data_uris
from ..orm import Application, Board, Job, utcnow
from ..vendors import VENDORS
from . import ServiceError
from .labels import label_filters
from .matching import term_regex

CANDIDATE_CAP = 2000
MAX_TERMS = 10
SNIPPET = 90


def parse_query(q: str | None) -> tuple[list[str], list[str]]:
    """-> (required terms, excluded terms)."""
    if not q or not q.strip():
        return [], []
    try:
        parts = shlex.split(q)
    except ValueError:  # unbalanced quote
        parts = q.split()
    req, neg = [], []
    for p in parts:
        if p.startswith("-") and len(p) > 1:
            neg.append(p[1:])
        else:
            req.append(p)
    if len(req) + len(neg) > MAX_TERMS:
        raise ServiceError(f"at most {MAX_TERMS} search terms")
    return req, neg


def _sub(term: str) -> str:
    return term.rstrip("*")


@dataclass
class Hit:
    job: Job
    in_title: bool
    snippet: str
    status: str | None  # this user's application status for the job, if any


def snippet(text: str, terms: list[str]) -> str:
    for t in terms:
        m = term_regex(t).search(text or "")
        if m:
            a, b = (
                max(0, m.start() - SNIPPET),
                min(len(text), m.end() + SNIPPET),
            )
            return (
                ("…" if a else "")
                + text[a:b].strip()
                + ("…" if b < len(text) else "")
            )
    return ""


def search_jobs(
    s: Session,
    user_id: int | None,
    title: str | None = None,
    keywords: str | None = None,
    match: str = "all",
    location: str | None = None,
    remote_only: bool = False,
    vendor: str | None = None,
    company: str | None = None,
    max_age_days: int | None = None,
    include_applied: bool = False,
    limit: int = 20,
    offset: int = 0,
    browse: bool = False,
    workplace: str | None = None,
    seniority=None,
    country=None,
    exclude_agencies: bool = False,
    sort: str = "relevance",
) -> dict:
    """`user_id=None` is an anonymous search: no ledger, so nothing is hidden
    and no status is shown.
    `browse=True` allows an empty query (just filters, newest first), which is
    how the jobs page lists.
    `sort` = 'relevance' (title hits first, then newest) | 'newest' (purely by
    publish date, falling back to when we first saw the job).
    `match` = 'all' | 'any' controls whether every keyword term, or at least
    one, must appear.
    Applied/skipped jobs are hidden unless include_applied (then each hit
    carries its status)."""
    if match not in ("all", "any"):
        raise ServiceError("match must be 'all' or 'any'")
    if sort not in ("relevance", "newest"):
        raise ServiceError("sort must be 'relevance' or 'newest'")
    if vendor and vendor not in VENDORS:
        raise ServiceError(
            f"unknown vendor {vendor!r}; known: {sorted(VENDORS)}"
        )
    t_req, t_neg = parse_query(title)
    k_req, k_neg = parse_query(keywords)
    if not (t_req or k_req) and not browse:
        raise ServiceError("give a title and/or keywords to search for")
    limit = min(max(limit, 1), 50)

    f = [Job.active == 1, Board.status == "enabled"]
    for t in t_req:
        f.append(Job.title.contains(_sub(t), autoescape=True))
    if k_req:
        per = [
            or_(
                Job.title.contains(_sub(t), autoescape=True),
                Job.description.contains(_sub(t), autoescape=True),
            )
            for t in k_req
        ]
        f.append(and_(*per) if match == "all" else or_(*per))
    if location:
        f.append(Job.location.contains(location, autoescape=True))
    if remote_only:
        workplace = "remote"
    f.extend(label_filters(workplace, seniority, country, exclude_agencies))
    if vendor:
        f.append(Job.vendor == vendor)
    if company:
        f.append(Job.company.contains(company, autoescape=True))
    if max_age_days:
        cutoff = utcnow() - timedelta(days=max_age_days)
        f.append(
            or_(
                Job.posted_at >= cutoff,
                (Job.posted_at.is_(None)) & (Job.first_seen >= cutoff),
            )
        )
    if user_id is not None and not include_applied:
        f.append(
            ~exists().where(
                Application.user_id == user_id, Application.job_id == Job.id
            )
        )

    if not (t_req or k_req):  # browse: SQL does all the work, so page in SQL
        # Count only the ids: wrapping the whole row (descriptions included)
        # in a subquery made MySQL read every posting's text.
        total = s.scalar(
            select(func.count(Job.id))
            .join(Board, Board.id == Job.board_id)
            .where(*f)
        )
        # Two steps: the ids of this page come from the covering index alone
        # (no row is read), then only those rows are loaded. The list never
        # shows a description, so it is not loaded either.
        ids = list(
            s.scalars(
                select(Job.id)
                .join(Board, Board.id == Job.board_id)
                .where(*f)
                .order_by(Job.sort_at.desc(), Job.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        by_id = {
            j.id: j
            for j in s.scalars(
                select(Job)
                .options(defer(Job.description), joinedload(Job.board))
                .where(Job.id.in_(ids))
            )
        }
        page = [Hit(by_id[i], False, "", None) for i in ids if i in by_id]
        return {
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "truncated": False,
            "hits": page,
        }

    rows = list(
        s.scalars(
            select(Job)
            .join(Board, Board.id == Job.board_id)
            .where(*f)
            .order_by(Job.sort_at.desc(), Job.id.desc())
            .limit(CANDIDATE_CAP)
        )
    )
    truncated = len(rows) == CANDIDATE_CAP

    t_rx = [term_regex(t) for t in t_req]
    k_rx = [term_regex(t) for t in k_req]
    n_rx = [term_regex(t) for t in t_neg + k_neg]
    hits: list[Hit] = []
    for j in rows:
        desc = strip_data_uris(j.description)  # pictures are not searchable
        both = f"{j.title}\n{desc}"
        if not all(r.search(j.title) for r in t_rx):
            continue
        if k_rx:
            found = [bool(r.search(both)) for r in k_rx]
            if not (all(found) if match == "all" else any(found)):
                continue
        if any(r.search(both) for r in n_rx):
            continue
        in_title = any(r.search(j.title) for r in t_rx + k_rx)
        hits.append(Hit(j, in_title, "", None))
    # Newest first by publish date either way. 'relevance' puts title hits
    # ahead of description-only hits, newest within each group.
    hits.sort(key=lambda h: (h.job.sort_at, h.job.id), reverse=True)
    if sort == "relevance":
        hits.sort(key=lambda h: not h.in_title)  # stable: keeps the order

    total = len(hits)
    page = hits[offset : offset + limit]
    statuses = {}
    if page and user_id is not None:
        statuses = dict(
            s.execute(
                select(Application.job_id, Application.status).where(
                    Application.user_id == user_id,
                    Application.job_id.in_([h.job.id for h in page]),
                )
            ).all()
        )
    for h in page:
        h.snippet = snippet(strip_data_uris(h.job.description), k_req or t_req)
        h.status = statuses.get(h.job.id)
    return {
        "total": total,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
        "truncated": truncated,
        "hits": page,
    }
