"""`livejobs scheduler`: the recurring jobs, for deployments with no cron
(containers).

* every hour, on the hour (UTC): fetch the latest job posts (what `sync-once
--slack 10` does);
* daily at 03:00 UTC on the 5th to the 12th of each month: refresh the Common
Crawl inventory, which is a no-op
  once the newest crawl is fully ingested (so: real work once a month, retried
  daily until it completes).

It is one process. Each job runs on its own thread and is skipped (not stacked)
if its previous run is still going,
so a slow Common Crawl refresh never delays the hourly fetch. Missed ticks
coalesce: after a long outage it runs once,
not once per missed hour.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from .orm import utcnow

log = logging.getLogger("livejobs.scheduler")

INVENTORY_DAYS = range(5, 13)  # 5th to 12th, inclusive
INVENTORY_HOUR = 3
CANARY_AT = (5, 30)  # daily, UTC: the live check of every platform


def next_hourly(now: datetime) -> datetime:
    """The next top of the hour strictly after `now`."""
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def next_inventory(now: datetime) -> datetime:
    """The next 03:00 on a day 5..12 strictly after `now`."""
    day = now.replace(hour=INVENTORY_HOUR, minute=0, second=0, microsecond=0)
    for _ in range(400):
        if day > now and day.day in INVENTORY_DAYS:
            return day
        day += timedelta(days=1)
    raise RuntimeError("no inventory date found")  # unreachable


def next_daily(now: datetime, hour: int, minute: int) -> datetime:
    """The next hour:minute strictly after `now`."""
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


class Scheduler:
    def __init__(
        self,
        sync_fn: Callable[[], object],
        inventory_fn: Callable[[], object],
        now: Callable[[], datetime] = utcnow,
        canary_fn: Callable[[], object] | None = None,
    ):
        self.jobs = {"sync": sync_fn, "inventory": inventory_fn}
        if canary_fn:
            self.jobs["canary"] = canary_fn
        self.now = now
        self._threads: dict[str, threading.Thread] = {}
        self._fired: datetime | None = None  # the last instant we fired at

    def _spawn(self, name: str) -> None:
        t = self._threads.get(name)
        if t and t.is_alive():
            log.warning(
                "%s is still running from its previous run; skipping this one",
                name,
            )
            return

        def run():
            try:
                log.info("%s starting", name)
                self.jobs[name]()
                log.info("%s finished", name)
            except (
                Exception
            ):  # a failed run is logged; the next scheduled run tries again
                log.exception("%s failed", name)

        self._threads[name] = threading.Thread(
            target=run, name=f"livejobs-{name}", daemon=True
        )
        self._threads[name].start()

    def run(
        self,
        stop: threading.Event,
        initial_sync: bool = True,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        """Blocks until `stop` is set. `wait(seconds) -> True if stopping` is a
        test seam (defaults to stop.wait)."""
        wait = wait or stop.wait
        if initial_sync:
            self._spawn(
                "sync"
            )  # boards enabled while we were down shouldn't wait up to an hour
        while not stop.is_set():
            # Waiting can return a hair before the wall-clock tick; never
            # schedule at or before what we already fired.
            now = max(self.now(), self._fired) if self._fired else self.now()
            due = {"sync": next_hourly(now), "inventory": next_inventory(now)}
            if "canary" in self.jobs:
                due["canary"] = next_daily(now, *CANARY_AT)
            name = min(due, key=due.get)
            log.info(
                "next: %s at %s UTC",
                name,
                due[name].isoformat(timespec="minutes"),
            )
            if wait(max(0.0, (due[name] - now).total_seconds())):
                return
            self._fired = due[name]
            self._spawn(name)
            if due["inventory"] == due["sync"] and name == "sync":
                self._spawn(
                    "inventory"
                )  # both fire at 03:00 on inventory days
        for t in (
            self._threads.values()
        ):  # let a run that is already in progress finish cleanly
            t.join(timeout=30)


def canary_job() -> Callable[[], object]:
    from . import canary, health

    def do_canary():
        results = canary.run()
        bad = [r["vendor"] for r in results if not r["ok"]]
        health.snapshot_vendors()
        health.log_anomalies(health.report())
        log.info(
            "canary done: %d platforms checked, failing: %s",
            len(results),
            ", ".join(bad) or "none",
        )

    return do_canary


def default_jobs() -> tuple[Callable[[], object], Callable[[], object]]:
    from . import health, inventory
    from . import config, db
    from .services import boards, sync

    def do_sync():
        with db.session() as s:
            boards.enable_discovered(s)
        res = sync.run_due(config.SYNC_WORKERS, slack_min=10, until_empty=True)
        log.info(
            "hourly sync done: %d boards, %d ok, %d jobs, %d new",
            len(res),
            sum(r["ok"] for r in res),
            sum(r["jobs"] or 0 for r in res),
            sum(r["new"] or 0 for r in res),
        )
        health.snapshot_vendors()  # today's counts, for the trend and alert

    def do_inventory():
        status, _ = inventory.refresh(None, config.CC_WORKERS, log=log.info)
        log.info("inventory refresh: %s", status)

    return do_sync, do_inventory
