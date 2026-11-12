"""Filters on the derived labels (workplace, seniority, country, agency),
shared by search and preference matching so the two always agree."""

from __future__ import annotations

from .. import classify
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


def label_filters(
    workplace=None, seniority=None, country=None, exclude_agencies=False
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
    return f
