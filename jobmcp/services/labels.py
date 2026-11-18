"""Filters on the derived labels (workplace, seniority, country, agency,
salary band),
shared by search and preference matching so the two always agree."""

from __future__ import annotations

from sqlalchemy import and_, or_

from .. import classify, salary
from ..orm import Board, Job
from . import ServiceError


def _list(v) -> list[str]:
    """A filter given as a list, or as 'a,b' (a URL query parameter)."""
    if v is None or v == "":
        return []
    if isinstance(v, str):
        v = v.split(",")
    return [x.strip() for x in v if isinstance(x, str) and x.strip()]


def tiers(v) -> list[str]:
    out = [x.lower() for x in _list(v)]
    bad = [x for x in out if x not in classify.SENIORITY]
    if bad:
        raise ServiceError(
            f"unknown seniority {bad}; use any of {list(classify.SENIORITY)}"
        )
    return out


def countries(v) -> list[str]:
    out = [x.upper() for x in _list(v)]
    bad = [x for x in out if not (len(x) == 2 and x.isalpha())]
    if bad:
        raise ServiceError(
            f"country must be a two-letter ISO code (US, GB, DE...); got {bad}"
        )
    return out


def salary_bands(v) -> list[str]:
    try:
        return salary.band_keys(_list(v))
    except ValueError as e:
        raise ServiceError(str(e)) from None


def salary_filter(bands: list[str]):
    """Postings whose stated pay overlaps any of the bands (an approximate
    USD per year, see salary.py). Postings with no stated pay never match."""
    parts = []
    for b in bands:
        lo, hi = salary.BANDS[b]
        cond = [Job.salary_usd_max >= lo]
        if hi is not None:
            cond.append(Job.salary_usd_min < hi)
        parts.append(and_(*cond))
    return or_(*parts)


def label_filters(
    workplace=None,
    seniority=None,
    country=None,
    exclude_agencies=False,
    salary_band=None,
) -> list:
    """SQL conditions for the derived labels, shared with preference matching
    so the two always agree."""
    f = []
    if workplace:
        if workplace not in ("remote", "hybrid"):
            raise ServiceError("workplace must be 'remote' or 'hybrid'")
        f.append(Job.workplace == workplace)
    if tiers(seniority):
        f.append(Job.seniority.in_(tiers(seniority)))
    if countries(country):
        f.append(Job.country.in_(countries(country)))
    if exclude_agencies:
        f.append(Board.is_agency == 0)
    if salary_bands(salary_band):
        f.append(salary_filter(salary_bands(salary_band)))
    return f
