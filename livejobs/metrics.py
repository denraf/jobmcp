"""Prometheus metrics.

Two kinds, on purpose:

* In-process counters/histograms (HTTP, auth events, emails, MCP tool calls).
These live in whichever
  process did the work, which for the web server is exactly what you want to
  scrape.
* Database-derived gauges and counters (users, boards, jobs, applications, sync
health). The sync runs in a
  *different* process (cron / the worker), so its numbers can't be in-process
  counters; they are read from
  the database at scrape time (cached for METRICS_CACHE_SECONDS) and are
  correct no matter which process wrote them.
"""

from __future__ import annotations

import calendar
import logging
import threading
import time
from datetime import timedelta

from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from sqlalchemy import func, select

from . import config, db
from .orm import (
    ApiToken,
    Application,
    Board,
    CanaryResult,
    InventoryRun,
    Job,
    SavedJob,
    SyncRun,
    User,
    utcnow,
)

log = logging.getLogger("livejobs.metrics")

HTTP_REQUESTS = Counter(
    "livejobs_http_requests_total",
    "HTTP requests served.",
    ["method", "handler", "status"],
)
HTTP_LATENCY = Histogram(
    "livejobs_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "handler"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
AUTH_EVENTS = Counter(
    "livejobs_auth_events_total", "Account events.", ["event"]
)
EMAILS = Counter(
    "livejobs_emails_total",
    "Emails by purpose and outcome.",
    ["purpose", "outcome"],
)
MCP_CALLS = Counter(
    "livejobs_mcp_tool_calls_total",
    "MCP tool calls by tool and outcome.",
    ["tool", "outcome"],
)

OVERDUE_AFTER = timedelta(
    minutes=30
)  # an enabled board this far past its due time is "overdue"


def _epoch(dt) -> float:
    return float(calendar.timegm(dt.timetuple())) if dt else 0.0


def _snapshot() -> dict:
    """One pass over the database. Cheap aggregate queries, all on indexed or
    small tables."""
    now = utcnow()
    with db.session() as s:
        out = {}
        out["users"] = s.execute(
            select(
                User.role, User.email_verified_at.is_not(None), func.count()
            ).group_by(User.role, User.email_verified_at.is_not(None))
        ).all()
        out["boards"] = s.execute(
            select(Board.vendor, Board.status, func.count()).group_by(
                Board.vendor, Board.status
            )
        ).all()
        out["by_source"] = s.execute(
            select(Board.source, Board.status, func.count()).group_by(
                Board.source, Board.status
            )
        ).all()
        out["failing"] = s.execute(
            select(Board.vendor, func.count())
            .where(Board.status == "enabled", Board.consecutive_failures > 0)
            .group_by(Board.vendor)
        ).all()
        out["overdue"] = (
            s.scalar(
                select(func.count())
                .select_from(Board)
                .where(
                    Board.status == "enabled",
                    Board.next_sync_at < now - OVERDUE_AFTER,
                )
            )
            or 0
        )
        out["jobs_active"] = s.execute(
            select(Job.vendor, func.count())
            .where(Job.active == 1)
            .group_by(Job.vendor)
        ).all()
        out["jobs_total"] = (
            s.scalar(select(func.count()).select_from(Job)) or 0
        )
        out["apps"] = s.execute(
            select(Application.status, func.count()).group_by(
                Application.status
            )
        ).all()
        out["saved"] = (
            s.scalar(select(func.count()).select_from(SavedJob)) or 0
        )
        out["mcp_tokens"] = (
            s.scalar(
                select(func.count())
                .select_from(ApiToken)
                .where(ApiToken.kind == "mcp", ApiToken.revoked_at.is_(None))
            )
            or 0
        )
        out["canary"] = [
            (c.vendor, c.ok, c.checked_at)
            for c in s.scalars(select(CanaryResult))
        ]
        out["last_ok"] = s.scalar(select(func.max(Board.last_ok_at)))
        out["last_run"] = s.scalar(select(func.max(SyncRun.finished_at)))
        out["runs"] = s.execute(
            select(SyncRun.ok, func.count())
            .where(SyncRun.finished_at.is_not(None))
            .group_by(SyncRun.ok)
        ).all()
        from . import health, inventory

        out["health"] = health.report()
        out["inv_running"] = (
            inventory.is_running()
        )  # the lock, not a row: a killed run leaves its row saying 'running'
        out["inv_ok"] = s.scalar(
            select(func.max(InventoryRun.finished_at)).where(
                InventoryRun.status == "complete"
            )
        )
        out["inv_last"] = s.scalar(select(func.max(InventoryRun.finished_at)))
        out["inv_runs"] = s.execute(
            select(InventoryRun.status, func.count())
            .where(InventoryRun.status != "running")
            .group_by(InventoryRun.status)
        ).all()
        out["inv_new"] = int(
            s.scalar(select(func.sum(InventoryRun.boards_new))) or 0
        )
        out["new"], out["closed"] = (
            int(x or 0)
            for x in s.execute(
                select(
                    func.sum(SyncRun.jobs_new), func.sum(SyncRun.jobs_closed)
                )
            ).one()
        )
    return out


class DbCollector:
    """Computed at scrape time. Cached so a burst of scrapes can't hammer the
    database."""

    def __init__(self):
        self._lock = threading.Lock()
        self._at = 0.0
        self._snap: dict | None = None

    def _get(self) -> dict | None:
        with self._lock:
            if (
                self._snap is None
                or time.monotonic() - self._at > config.METRICS_CACHE_SECONDS
            ):
                try:
                    self._snap, self._at = _snapshot(), time.monotonic()
                except Exception:
                    log.exception("metrics snapshot failed")
                    self._snap = None
            return self._snap

    def describe(self):
        # Without this, prometheus_client calls collect() at registration, i.e.
        # connects to the database on import.
        return []

    def reset(self) -> None:
        with self._lock:
            self._snap = None

    def collect(self):
        snap = self._get()
        up = GaugeMetricFamily(
            "livejobs_db_up", "1 if the database answered the metrics query."
        )
        up.add_metric([], 1.0 if snap else 0.0)
        yield up
        if not snap:
            return

        g = GaugeMetricFamily(
            "livejobs_users", "Registered users.", labels=["role", "verified"]
        )
        for role, verified, n in snap["users"]:
            g.add_metric([role, "true" if verified else "false"], n)
        yield g
        g = GaugeMetricFamily(
            "livejobs_boards",
            "Company job boards by platform and status.",
            labels=["vendor", "status"],
        )
        for vendor, status, n in snap["boards"]:
            g.add_metric([vendor, status], n)
        yield g
        g = GaugeMetricFamily(
            "livejobs_boards_by_source",
            "Boards by how they were added (crawl or manual) and status.",
            labels=["source", "status"],
        )
        for source, status, n in snap["by_source"]:
            g.add_metric([source, status], n)
        yield g
        g = GaugeMetricFamily(
            "livejobs_boards_failing",
            "Enabled boards whose last sync failed.",
            labels=["vendor"],
        )
        for vendor, n in snap["failing"]:
            g.add_metric([vendor], n)
        yield g
        g = GaugeMetricFamily(
            "livejobs_boards_overdue",
            "Enabled boards more than 30 minutes past their due sync time.",
        )
        g.add_metric([], snap["overdue"])
        yield g
        g = GaugeMetricFamily(
            "livejobs_jobs_active", "Open jobs by platform.", labels=["vendor"]
        )
        for vendor, n in snap["jobs_active"]:
            g.add_metric([vendor], n)
        yield g
        g = GaugeMetricFamily(
            "livejobs_vendor_anomaly",
            "1 if a platform's open postings are far outside their recent "
            "range (a drop usually means a parser or the vendor broke).",
            labels=["vendor", "direction"],
        )
        z = GaugeMetricFamily(
            "livejobs_vendor_jobs_zscore",
            "Standard deviations of today's open postings from the last 7 "
            "days, per platform.",
            labels=["vendor"],
        )
        for r in snap["health"]:
            if r["z"] is not None:
                z.add_metric([r["vendor"]], r["z"])
                for direction in ("drop", "spike"):
                    g.add_metric(
                        [r["vendor"], direction],
                        1.0 if r["state"] == direction else 0.0,
                    )
        yield g
        yield z
        g = GaugeMetricFamily(
            "livejobs_canary_ok",
            "1 if the daily live check of the platform found real postings.",
            labels=["vendor"],
        )
        t = GaugeMetricFamily(
            "livejobs_canary_checked_timestamp_seconds",
            "Unix time of the platform's last live check.",
            labels=["vendor"],
        )
        for vendor, ok, at in snap["canary"]:
            g.add_metric([vendor], 1.0 if ok else 0.0)
            t.add_metric([vendor], _epoch(at))
        yield g
        yield t
        g = GaugeMetricFamily(
            "livejobs_jobs_stored", "All jobs ever stored, open or closed."
        )
        g.add_metric([], snap["jobs_total"])
        yield g
        g = GaugeMetricFamily(
            "livejobs_applications",
            "Application ledger rows by status.",
            labels=["status"],
        )
        for status, n in snap["apps"]:
            g.add_metric([status], n)
        yield g
        g = GaugeMetricFamily("livejobs_saved_jobs", "Saved-job bookmarks.")
        g.add_metric([], snap["saved"])
        yield g
        g = GaugeMetricFamily(
            "livejobs_mcp_tokens_active", "Active (unrevoked) agent tokens."
        )
        g.add_metric([], snap["mcp_tokens"])
        yield g
        g = GaugeMetricFamily(
            "livejobs_sync_last_success_timestamp_seconds",
            "Unix time of the most recent successful board sync.",
        )
        g.add_metric([], _epoch(snap["last_ok"]))
        yield g
        g = GaugeMetricFamily(
            "livejobs_sync_last_run_timestamp_seconds",
            "Unix time the most recent sync run finished.",
        )
        g.add_metric([], _epoch(snap["last_run"]))
        yield g
        g = GaugeMetricFamily(
            "livejobs_inventory_last_success_timestamp_seconds",
            "Unix time the Common Crawl inventory last completed (0 = "
            "never). Refreshed monthly.",
        )
        g.add_metric([], _epoch(snap["inv_ok"]))
        yield g
        g = GaugeMetricFamily(
            "livejobs_inventory_running",
            "1 while a Common Crawl refresh is in progress.",
        )
        g.add_metric([], 1 if snap["inv_running"] else 0)
        yield g
        g = GaugeMetricFamily(
            "livejobs_inventory_last_run_timestamp_seconds",
            "Unix time of the last inventory attempt.",
        )
        g.add_metric([], _epoch(snap["inv_last"]))
        yield g
        c = CounterMetricFamily(
            "livejobs_inventory_runs",
            "Common Crawl inventory runs by status.",
            labels=["status"],
        )
        for status, n in snap["inv_runs"]:
            c.add_metric([status], n)
        yield c
        c = CounterMetricFamily(
            "livejobs_inventory_boards_new",
            "Boards first discovered by inventory runs.",
        )
        c.add_metric([], snap["inv_new"])
        yield c
        c = CounterMetricFamily(
            "livejobs_sync_runs",
            "Board sync runs by outcome.",
            labels=["outcome"],
        )
        for ok, n in snap["runs"]:
            c.add_metric(["ok" if ok else "error"], n)
        yield c
        c = CounterMetricFamily(
            "livejobs_sync_jobs_new", "Jobs first seen across all sync runs."
        )
        c.add_metric([], snap["new"])
        yield c
        c = CounterMetricFamily(
            "livejobs_sync_jobs_closed",
            "Jobs found closed across all sync runs.",
        )
        c.add_metric([], snap["closed"])
        yield c


collector = DbCollector()
REGISTRY.register(collector)


def authorized(client_host: str | None, headers) -> bool:
    """Who may read /metrics. A token (if configured) always works. Otherwise
    only loopback, and never a request that came
    through a proxy: a proxy on the same host also connects from 127.0.0.1, so
    X-Forwarded-For marks it as external."""
    token = config.METRICS_TOKEN
    if token:
        import hmac

        auth = headers.get("authorization", "")
        return auth.lower().startswith("bearer ") and hmac.compare_digest(
            auth[7:].strip(), token
        )
    if config.METRICS_PUBLIC:
        return True
    return client_host in ("127.0.0.1", "::1") and "x-forwarded-for" not in {
        k.lower() for k in headers.keys()
    }
