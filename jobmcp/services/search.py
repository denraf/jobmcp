"""Ad-hoc job search by title and/or any keyword in the title or description.

Query syntax (`keywords` / `title`): space-separated terms, all required;
  "machine learning"   quoted phrase
  engineer*            trailing * = prefix (matches engineering)
  -intern              exclude (whole word)
Matching is whole-word and case-insensitive, so 'go' does not match 'Google'.

Like preference matching this is two-stage: SQL narrows the candidates (at
most CANDIDATE_CAP), then Python applies the whole-word rules and ranks title
hits above description-only hits. `truncated` in the result says the cap was
hit, i.e. add filters or terms.

The narrowing is driven by MySQL FULLTEXT indexes (see orm.Job), so a query
over millions of postings reads only the rows that contain the words, and the
candidates are the BEST-scoring ones (title and company weighted above the
description), not merely the newest. A term the index cannot hold (under 3
characters such as 'go' or 'c', or a stop word) is checked with a substring
filter instead, exactly as before.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.mysql import match
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


# InnoDB ignores words shorter than innodb_ft_min_token_size (3) and its
# built-in stop words, so a boolean query that requires one matches nothing.
FT_MIN_TOKEN = 3
FT_STOPWORDS = frozenset(
    "a about an are as at be by com de en for from how i in is it la of on or "
    "that the this to was what when where who will with und the www".split()
)
_WORD = re.compile(r"\w+")


def ft_term(term: str) -> str | None:
    """One search term as a boolean-mode fragment, or None when the index
    cannot answer it. Only word characters ever reach the query, so a term
    can never inject an operator."""
    words = _WORD.findall(_sub(term))
    if not words or any(
        len(w) < FT_MIN_TOKEN or w.lower() in FT_STOPWORDS for w in words
    ):
        return None
    if len(words) == 1:
        return words[0] + ("*" if term.endswith("*") else "")
    if term.endswith("*"):  # a phrase cannot be a prefix: all its words
        return " ".join(f"+{w}" for w in words[:-1]) + f" +{words[-1]}*"
    return '"' + " ".join(words) + '"'


def ft_query(terms: list[str], any_term: bool = False) -> str | None:
    """Boolean-mode expression that narrows to rows containing the terms, or
    None for no narrowing. It only ever narrows: the whole-word check that
    follows decides. For 'all', the terms the index can hold are required and
    the rest are left to that check; for 'any', one unusable term means the
    index cannot narrow at all."""
    frags = [ft_term(t) for t in terms]
    if any_term:
        if not frags or None in frags:
            return None
        return " ".join(f.replace("+", "") for f in frags if f)
    usable = [f for f in frags if f]
    if not usable:
        return None
    return " ".join(f if f.startswith("+") else "+" + f for f in usable)


def _against(expr: str, *cols):
    return match(*cols, against=expr, in_boolean_mode=True)


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
    salary=None,
    visa=None,
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
    if not (t_req or k_req or (company and company.strip())) and not browse:
        raise ServiceError(
            "give a title, keywords and/or a company to search for"
        )
    limit = min(max(limit, 1), 50)

    f = [Job.active == 1, Board.status == "enabled"]
    # Narrowing by the full-text indexes comes first so MySQL starts from
    # them; the substring filters after it keep the exact semantics.
    title_ft = ft_query(t_req)
    text_ft = ft_query(k_req, match == "any")
    if title_ft:
        f.append(_against(title_ft, Job.title, Job.company))
    if text_ft:
        f.append(_against(text_ft, Job.title, Job.company, Job.description))
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
    f.extend(
        label_filters(
            workplace,
            seniority,
            country,
            exclude_agencies,
            salary_band=salary,
            visa=visa,
        )
    )
    if vendor:
        f.append(Job.vendor == vendor)
    if company:
        c_ft = ft_query([company.strip()])
        if c_ft:
            f.append(_against(c_ft, Job.company))
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

    # The best-scoring candidates, not just the newest: a title or company
    # hit counts three times a mention in the description.
    order = [Job.sort_at.desc(), Job.id.desc()]
    ranked = [
        _against(e, *cols) * w
        for e, cols, w in (
            (title_ft, (Job.title, Job.company), 3),
            (text_ft, (Job.title, Job.company, Job.description), 1),
        )
        if e
    ]
    if ranked:
        score = ranked[0] if len(ranked) == 1 else ranked[0] + ranked[1]
        order.insert(0, score.desc())
    rows = list(
        s.scalars(
            select(Job)
            .join(Board, Board.id == Job.board_id)
            .where(*f)
            .order_by(*order)
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


def search_companies(
    s: Session, query: str | None = None, limit: int = 20
) -> list[dict]:
    """Employers that have open jobs, biggest first: how an agent finds the
    exact name to pass as `company` (or sees who hires most). The boards table
    is one row per employer, so this never touches the jobs."""
    limit = min(max(limit, 1), 100)
    q = select(
        Board.company_name, func.sum(Board.job_count).label("open_jobs")
    ).where(
        Board.status == "enabled",
        Board.job_count > 0,
        Board.company_name != "",
    )
    if query and query.strip():
        q = q.where(
            Board.company_name.contains(query.strip(), autoescape=True)
        )
    rows = s.execute(
        q.group_by(Board.company_name)
        .order_by(func.sum(Board.job_count).desc(), Board.company_name)
        .limit(limit)
    ).all()
    return [{"company": n, "open_jobs": int(c)} for n, c in rows]
