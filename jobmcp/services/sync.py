"""Scheduled sync: claim due boards, fetch via the vendor parser, upsert jobs.

Network I/O happens outside any DB transaction, and each board is claimed with
an atomic UPDATE (a lease), so several workers can run side by side without
syncing the same board twice.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import requests
from sqlalchemy import func, select, update
from sqlalchemy.orm import defer

from .. import classify, config, db
from ..models import Job as Fetched
from ..models import fit
from ..orm import Board, Job, SyncRun, utcnow
from ..vendors import VENDORS
from ..vendors.base import BoardGone
from . import ServiceError

log = logging.getLogger("jobmcp.sync")
LEASE_MIN = 10

Fetcher = Callable[[str, str], list[Fetched]]


def previous_jobs(vendor: str, board: str) -> dict[str, Fetched]:
    """What is already stored for a board, by posting URL. Vendors whose list
    page carries no details (iCIMS) use it to fetch only the postings they
    have not seen."""
    out: dict[str, Fetched] = {}
    with db.session() as s:
        bid = s.scalar(
            select(Board.id).where(
                Board.vendor == vendor, Board.board == board
            )
        )
        if bid is None:
            return out
        for j in s.scalars(select(Job).where(Job.board_id == bid)):
            out[j.url] = Fetched(
                vendor=vendor,
                board=board,
                title=j.title,
                company=j.company,
                location=j.location,
                url=j.url,
                posted_at=j.posted_at.isoformat() if j.posted_at else "",
                description=j.description or "",
            )
    return out


def default_fetcher(vendor: str, board: str) -> list[Fetched]:
    v = VENDORS[vendor]
    if v.uses_previous:
        return v.fetch(board, previous=previous_jobs(vendor, board))
    return v.fetch(board)


def parse_dt(s: str | None) -> datetime | None:
    """Vendors return ISO strings, RFC 822 dates, or nothing. -> naive UTC."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    if d.tzinfo:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def interval_for(b: Board) -> int:
    return b.sync_interval_min or config.DEFAULT_SYNC_INTERVAL_MIN


def claim_due(
    limit: int,
    now: datetime | None = None,
    slack_min: int = 0,
    exclude: frozenset = frozenset(),
    force: bool = False,
) -> list[int]:
    """Atomically lease up to `limit` due boards. Returns the ids this caller
    won.

    `force` ignores the schedule and takes every enabled board (to re-fetch
    everything now, e.g. after a parser fix).
    `slack_min` also takes boards that will be due within that many minutes.
    Needed for cron: a board synced at
    10:00 on a 60-minute interval is next due at ~10:00-10:06 (jitter), so a
    cron firing at 11:00 would otherwise
    skip it and every board would end up syncing every *other* hour."""
    now = now or utcnow()
    horizon = now + timedelta(minutes=slack_min)
    schedule = (Board.next_sync_at.is_(None)) | (Board.next_sync_at <= horizon)
    if force:
        schedule = Board.id.is_not(None)
    won: list[int] = []
    log.debug(
        "claiming up to %d boards (slack=%dm, forced=%s, excluded=%d)",
        limit,
        slack_min,
        force,
        len(exclude),
    )
    with db.session() as s:
        ids = list(
            s.scalars(
                select(Board.id)
                .where(Board.status == "enabled", schedule)
                .order_by(
                    Board.next_sync_at.is_(None).desc(), Board.next_sync_at
                )
                .limit(limit + len(exclude))
            )
        )
        for bid in ids:
            if bid in exclude:
                continue
            if len(won) >= limit:
                break
            res = s.execute(
                update(Board)
                .where(
                    Board.id == bid,
                    Board.status == "enabled",
                    schedule,
                )
                .values(next_sync_at=now + timedelta(minutes=LEASE_MIN))
            )
            if res.rowcount == 1:
                won.append(bid)
    log.debug("claimed %d boards", len(won))
    return won


def sync_board(board_id: int, fetcher: Fetcher | None = None) -> dict:
    """Sync one board now, regardless of schedule. Never raises for fetch
    failures."""
    fetcher = fetcher or default_fetcher
    with db.session() as s:
        b = s.get(Board, board_id)
        if not b:
            raise ServiceError("board not found", 404)
        vendor, slug = b.vendor, b.board
        run = SyncRun(board_id=board_id)
        s.add(run)
        s.flush()
        run_id = run.id

    jobs: list[Fetched] = []
    error: str | None = None
    gone = False
    started = time.monotonic()
    log.debug("sync start %s/%s", vendor, slug)
    try:
        jobs = fetcher(vendor, slug)
    except BoardGone:
        error, gone = "gone", True
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        error, gone = f"HTTP {code}", code in (404, 410)
    except Exception as e:  # one broken board must not kill the worker
        error = f"{type(e).__name__}: {e}"[:500]
        log.debug("sync of %s/%s crashed", vendor, slug, exc_info=True)
    took = time.monotonic() - started

    with db.session() as s:
        b = s.get(Board, board_id)
        run = s.get(SyncRun, run_id)
        now = utcnow()
        b.last_sync_at = run.finished_at = now
        if error is None:
            seen, new, closed = _apply_jobs(s, b, jobs, now)
            b.job_count, b.last_error, b.last_ok_at = seen, None, now
            b.consecutive_failures = b.consecutive_gone = 0
            b.next_sync_at = now + timedelta(
                minutes=interval_for(b) * random.uniform(1.0, 1.1)
            )
            run.ok, run.jobs_seen, run.jobs_new, run.jobs_closed = (
                True,
                seen,
                new,
                closed,
            )
            if not b.company_name and jobs:
                b.company_name = jobs[0].company[:255]
            if b.agency_override is None:  # an admin's decision wins
                b.is_agency = classify.is_agency(b.company_name, b.board)
            else:
                b.is_agency = b.agency_override
            log.debug(
                "sync ok %s/%s: %d jobs, %d new, %d closed in %.2fs",
                vendor,
                slug,
                seen,
                new,
                closed,
                took,
            )
        else:
            b.last_error = run.error = error
            log.info(
                "sync failed %s/%s: %s (failure #%d, %.2fs)",
                vendor,
                slug,
                error,
                b.consecutive_failures + 1,
                took,
            )
            if gone:
                b.consecutive_gone += 1
                if b.consecutive_gone >= config.GONE_DISABLE_AFTER:
                    b.status = "disabled"
                    log.warning(
                        "disabled %s/%s: gone %d times in a row",
                        vendor,
                        slug,
                        b.consecutive_gone,
                    )
                    s.execute(
                        update(Job)
                        .where(Job.board_id == b.id, Job.active == 1)
                        .values(active=False)
                    )
                    b.job_count = 0
            b.consecutive_failures += 1
            backoff = min(
                interval_for(b) * 2 ** min(b.consecutive_failures, 8),
                config.MAX_BACKOFF_MIN,
            )
            b.next_sync_at = now + timedelta(minutes=max(backoff, 15))
        return {
            "board_id": board_id,
            "vendor": vendor,
            "board": slug,
            "ok": error is None,
            "error": error,
            "jobs": run.jobs_seen,
            "new": run.jobs_new,
            "closed": run.jobs_closed,
            "status": b.status,
        }


FLUSH_EVERY = (
    400  # rows per database flush; bounds what one big board holds in memory
)


def _apply_jobs(
    s, b: Board, fetched: list[Fetched], now: datetime
) -> tuple[int, int, int]:
    # a vendor can list the same posting twice
    by_uid = {j.uid: j for j in fetched}
    # The old description is never read (it is replaced below), so it is not
    # loaded: for a big board that would be a second full copy in memory.
    existing = {
        j.uid: j
        for j in s.scalars(
            select(Job)
            .where(Job.board_id == b.id)
            .options(defer(Job.description))
        )
    }
    new = 0
    pending: list[Job] = []

    def flush() -> None:
        # Write what we have and let go of it: a board with thousands of
        # postings is stored in slices, not held whole.
        s.flush()
        for row in pending:
            s.expunge(row)
        pending.clear()

    for uid, f in by_uid.items():
        row = existing.get(uid)
        if row:
            row.last_seen, row.active = now, True
            row.location = f.location[:500]
            if row.posted_at is None and (posted := parse_dt(f.posted_at)):
                row.posted_at, row.sort_at = posted, min(posted, now)
            row.workplace = classify.workplace(f.location, f.title)
            row.seniority = classify.seniority(f.title)
            row.country = classify.country(f.location)
            if f.description:  # keep the stored one if this fetch had none
                row.description = fit(f.description)
        else:
            posted = parse_dt(f.posted_at)
            row = Job(
                uid=uid,
                board_id=b.id,
                vendor=b.vendor,
                title=f.title[:500],
                company=f.company[:255],
                location=f.location[:500],
                url=f.url[:1000],
                posted_at=posted,
                sort_at=min(posted or now, now),
                workplace=classify.workplace(f.location, f.title),
                seniority=classify.seniority(f.title),
                country=classify.country(f.location),
                description=fit(f.description),
                first_seen=now,
                last_seen=now,
            )
            s.add(row)
            new += 1
        pending.append(row)
        if len(pending) >= FLUSH_EVERY:
            flush()
    flush()
    closed = 0
    for uid, row in existing.items():
        if uid not in by_uid and row.active:
            row.active = False
            closed += 1
    return len(by_uid), new, closed


def sync_many(
    ids,
    workers: int = config.SYNC_WORKERS,
    fetcher: Fetcher | None = None,
    background: bool = False,
    known: dict | None = None,
) -> list[dict]:
    """Sync these boards in parallel on a thread pool. `ids` may be a dict
    {board id: jobs already fetched}, in which
    case those boards are stored from the jobs in hand instead of being fetched
    again. With `background` the work
    runs on a daemon thread and this returns at once (what a web request
    wants)."""
    known = (
        known if known is not None else (ids if isinstance(ids, dict) else {})
    )
    ids = list(ids)

    def one(i: int) -> dict:
        jobs = known.pop(i, None)  # let go of them as soon as they are stored
        return sync_board(
            i, (lambda v, b: jobs) if jobs is not None else fetcher
        )

    def go() -> list[dict]:
        pool = max(1, min(workers, len(ids)))
        log.debug("syncing %d boards on %d threads", len(ids), pool)
        started = time.monotonic()
        with ThreadPoolExecutor(pool, thread_name_prefix="sync") as ex:
            res = list(ex.map(one, ids))
        if res:
            log.info(
                "synced %d boards in %.1fs: %d ok, %d failed, %d jobs, %d new",
                len(res),
                time.monotonic() - started,
                sum(r["ok"] for r in res),
                sum(not r["ok"] for r in res),
                sum(r["jobs"] or 0 for r in res),
                sum(r["new"] or 0 for r in res),
            )
        return res

    if background:
        threading.Thread(
            target=go, name="jobmcp-sync-new", daemon=True
        ).start()
        return []
    return go()


def start_background(workers: int = config.SYNC_WORKERS) -> threading.Thread:
    """Sync everything that is due, on a daemon thread. Safe next to the
    scheduler: boards are leased."""

    def go():
        try:
            res = run_due(workers, until_empty=True)
            log.info("background sync finished: %d boards", len(res))
        except Exception:
            log.exception("background sync failed")

    t = threading.Thread(target=go, name="jobmcp-sync-due", daemon=True)
    t.start()
    return t


def run_due(
    workers: int = config.SYNC_WORKERS,
    batch: int = 200,
    fetcher: Fetcher | None = None,
    slack_min: int = 0,
    until_empty: bool = False,
    force: bool = False,
) -> list[dict]:
    """Sync due boards. `until_empty` keeps claiming batches until nothing is
    due (what a cron run wants)."""
    results: list[dict] = []
    done: set[int] = (
        set()
        # a short-interval board could fall inside the slack window again right
        # after syncing
    )
    while True:
        ids = claim_due(
            batch, slack_min=slack_min, exclude=frozenset(done), force=force
        )
        if not ids:
            return results
        done.update(ids)
        results += sync_many(ids, workers, fetcher)
        if not until_empty:
            return results


_all_lock = threading.Lock()
_all: dict | None = None


def sync_all_state() -> dict | None:
    with _all_lock:
        return dict(_all) if _all else None


def start_all(
    workers: int = config.SYNC_WORKERS, fetcher: Fetcher | None = None
) -> dict:
    """Refresh every enabled board now, whatever its schedule, on a background
    thread. Returns at once; poll
    `sync_all_state()`. Boards are taken in small batches through the same
    lease as the scheduler, so it is safe next
    to the scheduler (a board someone else is syncing is skipped) and progress
    is steady. One run at a time."""
    global _all
    with _all_lock:
        if _all and _all["state"] == "running":
            raise ServiceError(
                "A sync of all boards is already running. Its progress is "
                "shown above.",
                409,
            )
        with db.session() as s:
            total = (
                s.scalar(
                    select(func.count())
                    .select_from(Board)
                    .where(Board.status == "enabled")
                )
                or 0
            )
        state = {
            "state": "running",
            "total": total,
            "done": 0,
            "ok": 0,
            "failed": 0,
            "jobs": 0,
            "new": 0,
            "started_at": utcnow(),
            "finished_at": None,
            "error": None,
        }
        _all = state

    def run() -> None:
        done: set[int] = set()
        started = time.monotonic()
        log.info("sync of all %d enabled boards started", total)
        try:
            while True:
                ids = claim_due(
                    max(workers * 4, 16), exclude=frozenset(done), force=True
                )
                if not ids:
                    break
                done.update(ids)
                res = sync_many(ids, workers, fetcher)
                with _all_lock:
                    state["done"] = len(done)
                    state["ok"] += sum(r["ok"] for r in res)
                    state["failed"] += sum(not r["ok"] for r in res)
                    state["jobs"] += sum(r["jobs"] or 0 for r in res)
                    state["new"] += sum(r["new"] or 0 for r in res)
        except Exception as e:
            log.exception("sync of all boards failed")
            state["error"] = f"{type(e).__name__}: {e}"[:300]
        with _all_lock:
            state["total"] = max(
                state["total"], state["done"]
            )  # boards added meanwhile
            state["state"], state["finished_at"] = (
                ("failed" if state["error"] else "done"),
                utcnow(),
            )
        log.info(
            "sync of all boards finished: %d boards, %d ok, %d failed in "
            "%.0fs",
            state["done"],
            state["ok"],
            state["failed"],
            time.monotonic() - started,
        )

    threading.Thread(target=run, name="jobmcp-sync-all", daemon=True).start()
    return sync_all_state()


def run_forever(
    stop: threading.Event,
    workers: int = config.SYNC_WORKERS,
    batch: int = 200,
    poll_seconds: int = 30,
    fetcher: Fetcher | None = None,
) -> None:
    log.info(
        "sync worker started (workers=%d, poll=%ds)", workers, poll_seconds
    )
    while not stop.is_set():
        try:
            results = run_due(workers, batch, fetcher)
            if results:
                # more may be due; don't sleep while there's a backlog
                continue
        except Exception:
            log.exception("sync tick failed")
        stop.wait(poll_seconds)
