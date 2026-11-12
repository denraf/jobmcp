"""Build the board inventory from Common Crawl's CDX index.

For each vendor we query its hostnames (matchType=domain), page through every
URL captured, and reduce each URL to a board key with the vendor's own
`board_from_url`. Common Crawl's index server is slow and returns 504s on big
domains, so every page is retried and recorded in `crawl_progress` — an
interrupted run resumes where it stopped.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select, text

from . import config, db, http
from .orm import Board, CrawlProgress, InventoryRun, utcnow
from .services import boards as boards_svc
from .vendors import VENDORS

_log = logging.getLogger("livejobs.inventory")
CC = "https://index.commoncrawl.org"
_gate = http.Gate(
    "Common Crawl", config.CC_MIN_INTERVAL, trip_after=config.CC_GIVE_UP_AFTER
)


def list_crawls(n: int) -> list[str]:
    r = http.get(f"{CC}/collinfo.json", gate=_gate, session=http.crawl_session)
    r.raise_for_status()
    return [c["id"] for c in r.json()[:n]]  # newest first


def num_pages(crawl: str, url: str, match: str) -> int:
    r = http.get(
        f"{CC}/{crawl}-index",
        params={
            "url": url,
            "matchType": match,
            "output": "json",
            "showNumPages": "true",
        },
        retries=config.CC_RETRIES,
        timeout=120,
        gate=_gate,
        session=http.crawl_session,
    )
    if r.status_code == 404:
        return 0
    r.raise_for_status()
    return int(r.json()["pages"])


def fetch_page(crawl: str, url: str, match: str, page: int) -> list[str]:
    _log.debug("fetching %s page %d of %s", crawl, page, url)
    r = http.get(
        f"{CC}/{crawl}-index",
        params={
            "url": url,
            "matchType": match,
            "output": "json",
            "fl": "url",
            "filter": "status:200",
            "page": page,
        },
        retries=config.CC_RETRIES,
        timeout=180,
        gate=_gate,
        session=http.crawl_session,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    urls = []
    for line in r.text.splitlines():
        try:
            urls.append(json.loads(line)["url"])
        except (ValueError, KeyError):
            continue  # CDX sometimes emits a truncated final line
    return urls


def build(
    crawls: list[str],
    vendors: list[str] | None = None,
    workers: int = config.CC_WORKERS,
    log=_log.info,
    fetch=None,
    pages_of=None,
    progress=None,
) -> dict:
    """Ingest the index pages not yet ingested for these crawls. Resumable:
    pages already recorded in
    `crawl_progress` are skipped, so re-running after a failure (or after a
    finished run) only does what is left.

    Discovered boards land as status='discovered': nothing is synced until an
    admin enables it.
    workers is kept low on purpose: index.commoncrawl.org asks for politeness.
    Returns {"fetched": pages ingested, "failed": pages or queries that
    errored, "total": pages found to fetch}.
    `progress(stats)` is called after every page. `fetch` / `pages_of` are test
    seams.
    """
    fetch, pages_of = fetch or fetch_page, pages_of or num_pages
    stats = {"fetched": 0, "failed": 0, "total": 0, "gave_up": False}
    _gate.reset()

    def down() -> bool:
        if not _gate.tripped():
            return False
        if not stats["gave_up"]:
            stats["gave_up"] = True
            log(
                f"  ! Common Crawl failed {config.CC_GIVE_UP_AFTER} times in "
                "a row; stopping. Progress is saved: the next run resumes."
            )
        return True

    for crawl in crawls:
        if down():
            break
        for name, vendor in VENDORS.items():
            if vendors and name not in vendors:
                continue
            for url, match in vendor.cdx_queries:
                if down():
                    return stats
                try:
                    total = pages_of(crawl, url, match)
                # the index server 504s on big domains; one bad query must not
                # abort the rest
                except Exception as e:
                    log(
                        f"  ! [{crawl}] {name} {url}: "
                        f"could not list pages: {e}"
                    )
                    stats["failed"] += 1
                    progress and progress(stats)
                    continue
                with db.session() as s:
                    done = set(
                        s.scalars(
                            select(CrawlProgress.page).where(
                                CrawlProgress.crawl == crawl,
                                CrawlProgress.vendor == name,
                                CrawlProgress.query == url,
                            )
                        )
                    )
                todo = [p for p in range(total) if p not in done]
                log(
                    f"[{crawl}] {name} {url}: {total} pages, "
                    f"{len(todo)} to fetch"
                )
                stats["total"] += len(todo)
                progress and progress(stats)

                def work(p, vendor=vendor, url=url, match=match, crawl=crawl):
                    try:
                        urls = fetch(crawl, url, match, p)
                    except Exception as e:
                        log(f"  ! page {p}: {e}")
                        return p, None
                    return p, Counter(
                        b for b in map(vendor.board_from_url, urls) if b
                    )

                with ThreadPoolExecutor(
                    workers, thread_name_prefix="crawl"
                ) as ex:  # never the sync pool
                    for p, boards in ex.map(work, todo):
                        if boards is None:
                            down()
                            stats["failed"] += 1
                            progress and progress(stats)
                            continue
                        with db.session() as s:
                            boards_svc.upsert_discovered(s, name, boards)
                            s.add(
                                CrawlProgress(
                                    crawl=crawl,
                                    vendor=name,
                                    query=url,
                                    page=p,
                                    num_pages=total,
                                )
                            )
                        stats["fetched"] += 1
                        progress and progress(stats)
                if down():
                    return stats
    return stats


def lock_name() -> str:
    """MySQL lock names are server-wide, not per database. Include the
    database, so two livejobs installs that share
    a MySQL server (dev and staging, say) never block each other's refresh.
    (Names are limited to 64 characters.)"""
    import hashlib

    d = db.engine().url
    return (
        "livejobs:inventory:"
        + hashlib.sha1(f"{d.host}:{d.port}/{d.database}".encode()).hexdigest()[
            :24
        ]
    )


class RefreshLock:
    """One refresh at a time, across every process and container (the monthly
    scheduler, a CLI run and the web button
    all go through this). A MySQL named lock: it belongs to a connection, so it
    frees itself if the process dies."""

    def __init__(self):
        self.conn = None

    def acquire(self) -> bool:
        conn = db.engine().connect()
        if (
            conn.execute(
                text("SELECT GET_LOCK(:n, 0)"), {"n": lock_name()}
            ).scalar()
            == 1
        ):
            self.conn = conn
            return True
        conn.close()
        return False

    def ping(self) -> None:
        """Keep the connection (and so the lock) alive: a run can be idle on it
        for a long time."""
        if self.conn is not None:
            self.conn.execute(text("SELECT 1"))

    def release(self) -> None:
        conn, self.conn = self.conn, None
        if conn is None:
            return
        try:
            conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": lock_name()})
        except Exception:
            conn.invalidate()  # a dead connection has already lost the lock
        finally:
            conn.close()


def is_running() -> bool:
    """Is a refresh genuinely in progress, in any process? True only while the
    named lock is held, which a row in the
    database cannot tell you: a process that was killed leaves its row saying
    'running' forever."""
    with db.engine().connect() as conn:
        return (
            conn.execute(
                text("SELECT IS_USED_LOCK(:n)"), {"n": lock_name()}
            ).scalar()
            is not None
        )


def reconcile_stale(session=None) -> int:
    """Close the books on runs whose process died: rows still 'running' while
    no refresh holds the lock.
    Safe to call at any time (a live run always holds the lock until after it
    has written its final status).

    Pass the session the caller will read with: MySQL's default isolation
    (REPEATABLE READ) pins a transaction to the
    snapshot of its first read, so a caller that already queried would not see
    an update made on another session."""
    if is_running():
        return 0

    def close(s) -> int:
        stale = list(
            s.scalars(
                select(InventoryRun).where(InventoryRun.status == "running")
            )
        )
        for r in stale:
            r.status, r.finished_at = "failed", utcnow()
            r.error = (
                "interrupted: the process stopped before this run finished "
                "(starting a refresh again resumes where it left off)"
            )
        s.flush()
        return len(stale)

    if session is not None:
        return close(session)
    with db.session() as s:
        return close(s)


def _newest_crawl(
    crawls_fn, log, trigger: str
) -> tuple[str | None, InventoryRun | None]:
    try:
        return (crawls_fn or list_crawls)(1)[0], None
    except Exception as e:
        with db.session() as s:
            run = InventoryRun(
                crawl="?",
                finished_at=utcnow(),
                status="failed",
                trigger=trigger,
                error=f"{type(e).__name__}: {e}"[:500],
            )
            s.add(run)
        log(f"could not list Common Crawl indexes: {e}")
        return None, run


def _already_ingested(crawl: str) -> bool:
    with db.session() as s:
        return bool(
            s.scalar(
                select(InventoryRun.id)
                .where(
                    InventoryRun.crawl == crawl,
                    InventoryRun.status == "complete",
                )
                .limit(1)
            )
        )


def _ingest(
    crawl: str, lock: RefreshLock, vendors, workers, log, trigger: str, **seams
) -> tuple[str, InventoryRun]:
    """Do the run. The caller holds `lock`; it is released here, whatever
    happens."""
    try:
        with db.session() as s:
            # We hold the lock, so nothing is genuinely running: any 'running'
            # row is from a process that died.
            for stale in s.scalars(
                select(InventoryRun).where(InventoryRun.status == "running")
            ):
                stale.status, stale.finished_at = "failed", utcnow()
                stale.error = (
                    "interrupted: the process stopped before this run "
                    "finished (starting a refresh again resumes where it left "
                    "off)"
                )
            run = InventoryRun(crawl=crawl, trigger=trigger)
            s.add(run)
            s.flush()
            run_id = run.id
            boards_before = (
                s.scalar(select(func.count()).select_from(Board)) or 0
            )

        def on_progress(st):
            lock.ping()
            with db.session() as s:
                r = s.get(InventoryRun, run_id)
                r.pages_fetched, r.pages_failed, r.pages_total = (
                    st["fetched"],
                    st["failed"],
                    st["total"],
                )
                # keep this current too, so a run that is interrupted still
                # reports what it found
                r.boards_new = max(
                    0,
                    (s.scalar(select(func.count()).select_from(Board)) or 0)
                    - boards_before,
                )

        try:
            stats = build(
                [crawl], vendors, workers, log, progress=on_progress, **seams
            )
            error = None
        except Exception as e:  # e.g. the database went away mid-run
            stats, error = (
                {"fetched": 0, "failed": 1, "total": 0},
                f"{type(e).__name__}: {e}"[:500],
            )
            log(f"inventory run crashed: {e}")
            _log.debug("inventory run crashed", exc_info=True)
        with db.session() as s:
            run = s.get(InventoryRun, run_id)
            run.finished_at = utcnow()
            run.pages_fetched, run.pages_failed, run.pages_total, run.error = (
                stats["fetched"],
                stats["failed"],
                stats["total"],
                error,
            )
            run.boards_new = max(
                0,
                (s.scalar(select(func.count()).select_from(Board)) or 0)
                - boards_before,
            )
            run.status = (
                "failed"
                if error
                else ("complete" if stats["failed"] == 0 else "partial")
            )
            status = run.status
        log(
            f"{crawl}: {status}, {stats['fetched']} pages ingested, "
            f"{stats['failed']} failed"
        )
        if config.SYNC_AFTER_REFRESH and run.boards_new:
            from .services import sync

            # new boards are already enabled and due; don't make them wait for
            # the next hourly run
            sync.start_background()
        return status, run
    finally:
        lock.release()


def refresh(
    vendors: list[str] | None = None,
    workers: int = config.CC_WORKERS,
    log=_log.info,
    crawls_fn=None,
    trigger: str = "scheduled",
    **seams,
) -> tuple[str, InventoryRun | None]:
    """The monthly job. Ingest the newest Common Crawl index unless it is
    already fully ingested.

    Idempotent on purpose, so it can be scheduled on several days: Common Crawl
    publishes about one crawl a month,
    and this does real work once per crawl (and again, resuming, if a run was
    cut short by the index server's 504s).
    -> ("up_to_date" | "complete" | "partial" | "failed" | "already_running",
    the InventoryRun row or None)."""
    crawl, failed = _newest_crawl(crawls_fn, log, trigger)
    if crawl is None:
        return "failed", failed
    if _already_ingested(crawl):
        log(f"{crawl} is already fully ingested; nothing to do")
        return "up_to_date", None
    lock = RefreshLock()
    if not lock.acquire():
        log("another refresh is already running; not starting a second one")
        return "already_running", None
    return _ingest(crawl, lock, vendors, workers, log, trigger, **seams)


def start_background(
    vendors: list[str] | None = None,
    workers: int = config.CC_WORKERS,
    log=_log.info,
    crawls_fn=None,
    trigger: str = "web",
    **seams,
) -> dict:
    """What the admin's button calls. Decides quickly, then returns while the
    run continues on a thread:
    {"status": "started" | "up_to_date" | "already_running" | "failed",
    "crawl": ..., "error": ...}."""
    import threading

    crawl, failed = _newest_crawl(crawls_fn, log, trigger)
    if crawl is None:
        return {"status": "failed", "crawl": None, "error": failed.error}
    if _already_ingested(crawl):
        log(f"{crawl} is already fully ingested; nothing to do")
        return {"status": "up_to_date", "crawl": crawl}
    lock = RefreshLock()
    if not lock.acquire():
        log("another refresh is already running; not starting a second one")
        return {"status": "already_running", "crawl": crawl}
    _log.info(
        "refresh of %s started in the background (trigger=%s)", crawl, trigger
    )
    t = threading.Thread(
        target=_ingest,
        args=(crawl, lock, vendors, workers, log, trigger),
        kwargs=seams,
        name="livejobs-inventory",
        daemon=True,
    )
    t.start()
    return {"status": "started", "crawl": crawl, "thread": t}
