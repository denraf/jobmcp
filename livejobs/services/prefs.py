"""Job preferences: the only per-user settings the service stores (besides
login)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..orm import Preferences, User
from ..vendors import VENDORS
from . import ServiceError, labels

PREF_LISTS = (
    "title_include",
    "title_exclude",
    "locations",
    "vendors",
    "companies_include",
    "companies_exclude",
    "description_exclude",
)


def _strings(v, field: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ServiceError(f"{field} must be a list of strings")
    seen: dict[str, None] = {}
    for x in v:
        x = x.strip()
        if x:
            seen[x] = None
    if len(seen) > 200:
        raise ServiceError(f"{field}: at most 200 entries")
    return list(seen)


def prefs_dict(p: Preferences) -> dict:
    d = {k: list(getattr(p, k) or []) for k in PREF_LISTS}
    d.update(
        seniority=list(p.seniority or []),
        countries=list(p.countries or []),
        exclude_agencies=bool(p.exclude_agencies),
        remote_only=p.remote_only,
        max_age_days=p.max_age_days,
        max_applications_per_day=p.max_applications_per_day,
    )
    return d


def get_preferences(s: Session, user: User) -> Preferences:
    if user.preferences is None:
        user.preferences = Preferences()
        s.flush()
    return user.preferences


def update_preferences(s: Session, user: User, data: dict) -> Preferences:
    p = get_preferences(s, user)
    for k, v in data.items():
        if k in PREF_LISTS:
            v = _strings(v, k)
            if k == "vendors":
                bad = [x for x in v if x not in VENDORS]
                if bad:
                    raise ServiceError(
                        f"unknown vendors {bad}; known: {sorted(VENDORS)}"
                    )
            setattr(p, k, v)
        elif k == "remote_only":
            p.remote_only = bool(v)
        elif k == "exclude_agencies":
            p.exclude_agencies = bool(v)
        elif k == "seniority":
            p.seniority = labels.tiers(_strings(v, k))
        elif k == "countries":
            p.countries = labels.countries(_strings(v, k))
        elif k == "max_age_days":
            if v is not None and (not isinstance(v, int) or v < 1):
                raise ServiceError(
                    "max_age_days must be a positive integer or null"
                )
            p.max_age_days = v
        elif k == "max_applications_per_day":
            if not isinstance(v, int) or not 0 <= v <= 500:
                raise ServiceError("max_applications_per_day must be 0..500")
            p.max_applications_per_day = v
        else:
            raise ServiceError(f"unknown preference {k!r}")
    return p
