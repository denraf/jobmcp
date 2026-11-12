"""Platform health: a daily count of open postings per vendor, and a check
that flags a vendor whose count falls off (or jumps) far outside its own
recent range. That is what an upstream change looks like from here: a parser
that quietly returns nothing, a vendor that starts blocking us."""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func, select

from . import db
from .orm import Board, CanaryResult, Job, VendorSnapshot, utcnow
from .vendors import VENDORS

log = logging.getLogger("livejobs.health")

WINDOW_DAYS = 7  # how much earlier history the check compares against
MIN_DAYS = 5  # fewer earlier days than this: still learning, no verdict
THRESHOLD = 3.0  # standard deviations from the recent mean
MIN_CHANGE = 0.10  # ...and at least this fraction of the mean, so a perfectly
MIN_ABS = 10  # flat history does not flag a change of two postings


def _day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def snapshot_vendors(now: datetime | None = None) -> int:
    """Record today's open postings per vendor. Safe to call any number of
    times a day: the day's row is overwritten with the latest figures."""
    now = now or utcnow()
    with db.session() as s:
        jobs = dict(
            s.execute(
                select(Job.vendor, func.count())
                .join(Board, Board.id == Job.board_id)
                .where(Job.active == 1, Board.status == "enabled")
                .group_by(Job.vendor)
            ).all()
        )
        enabled = dict(
            s.execute(
                select(Board.vendor, func.count())
                .where(Board.status == "enabled")
                .group_by(Board.vendor)
            ).all()
        )
        failing = dict(
            s.execute(
                select(Board.vendor, func.count())
                .where(
                    Board.status == "enabled", Board.consecutive_failures > 0
                )
                .group_by(Board.vendor)
            ).all()
        )
        for vendor in VENDORS:
            if not enabled.get(vendor) and not jobs.get(vendor):
                continue  # a platform with no boards has no history to keep
            s.merge(
                VendorSnapshot(
                    day=_day(now),
                    vendor=vendor,
                    active_jobs=int(jobs.get(vendor, 0)),
                    boards_enabled=int(enabled.get(vendor, 0)),
                    boards_failing=int(failing.get(vendor, 0)),
                    taken_at=now,
                )
            )
    return len(VENDORS)


def judge(history: list[int], today: int) -> dict:
    """Is `today` out of line with `history` (earlier days, oldest first)?
    -> {state: learning|ok|drop|spike, mean, std, z, change}"""
    out = {
        "state": "learning",
        "mean": None,
        "std": None,
        "z": None,
        "change": None,
    }
    if len(history) < MIN_DAYS:
        return out
    mean = statistics.fmean(history)
    std = statistics.pstdev(history)
    delta = today - mean
    z = delta / max(std, 1.0)
    change = delta / mean if mean else 0.0
    out.update(
        mean=round(mean, 1),
        std=round(std, 1),
        z=round(z, 2),
        change=round(change, 3),
    )
    out["state"] = "ok"
    if abs(z) >= THRESHOLD and abs(delta) >= max(MIN_ABS, MIN_CHANGE * mean):
        out["state"] = "drop" if delta < 0 else "spike"
    return out


def report(now: datetime | None = None, days: int = 30) -> list[dict]:
    """One row per vendor that has history: today's figure, the verdict, a
    short series for a chart, and the latest canary result."""
    now = now or utcnow()
    today = _day(now)
    with db.session() as s:
        rows = list(
            s.scalars(
                select(VendorSnapshot)
                .where(VendorSnapshot.day >= today - timedelta(days=days))
                .order_by(VendorSnapshot.vendor, VendorSnapshot.day)
            )
        )
        canary = {c.vendor: c for c in s.scalars(select(CanaryResult))}
    by_vendor: dict[str, list[VendorSnapshot]] = {}
    for r in rows:
        by_vendor.setdefault(r.vendor, []).append(r)
    out = []
    for vendor in sorted(set(by_vendor) | set(canary)):
        snaps = by_vendor.get(vendor, [])
        c = canary.get(vendor)
        row = {
            "vendor": vendor,
            "label": VENDORS[vendor].label or vendor,
            "active_jobs": None,
            "boards_enabled": 0,
            "boards_failing": 0,
            "as_of": None,
            "state": "learning",
            "mean": None,
            "std": None,
            "z": None,
            "change": None,
            "series": [],
            "canary": (
                {
                    "ok": c.ok,
                    "board": c.board,
                    "jobs": c.jobs,
                    "error": c.error,
                    "checked_at": c.checked_at,
                }
                if c
                else None
            ),
        }
        if snaps:
            latest = snaps[-1]
            earlier = [
                x.active_jobs
                for x in snaps
                if latest.day - timedelta(days=WINDOW_DAYS)
                <= x.day
                < latest.day
            ]
            row.update(
                active_jobs=latest.active_jobs,
                boards_enabled=latest.boards_enabled,
                boards_failing=latest.boards_failing,
                as_of=latest.taken_at,
                series=[
                    {"day": x.day.date().isoformat(), "jobs": x.active_jobs}
                    for x in snaps
                ],
                **judge(earlier, latest.active_jobs),
            )
        out.append(row)
    out.sort(key=lambda r: r["vendor"])
    return out


def log_anomalies(rows: list[dict]) -> None:
    for r in rows:
        if r["state"] in ("drop", "spike"):
            log.warning(
                "%s open postings look wrong: %d today vs about %.0f usually "
                "(%+.0f%%, z=%.1f)",
                r["vendor"],
                r["active_jobs"],
                r["mean"],
                100 * r["change"],
                r["z"],
            )
