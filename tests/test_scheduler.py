"""The recurring jobs, tested with a fake clock: nothing sleeps."""

import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from jobmcp import cli, db, scheduler
from jobmcp.orm import User
from jobmcp.scheduler import Scheduler, next_hourly, next_inventory


D = datetime


def test_next_hourly_is_the_next_top_of_the_hour():
    assert next_hourly(D(2026, 11, 10, 10, 0, 0)) == D(
        2026, 11, 10, 11
    )  # strictly after
    assert next_hourly(D(2026, 11, 10, 10, 59, 59)) == D(2026, 11, 10, 11)
    assert next_hourly(D(2026, 11, 10, 23, 30)) == D(2026, 11, 11, 0)
    assert next_hourly(D(2026, 12, 31, 23, 30)) == D(2027, 1, 1, 0)


@pytest.mark.parametrize(
    "now,expected",
    [
        (D(2026, 11, 1, 12), D(2026, 11, 5, 3)),  # before the window: the 5th
        (D(2026, 11, 5, 2, 59), D(2026, 11, 5, 3)),
        (D(2026, 11, 5, 3, 0), D(2026, 11, 6, 3)),  # strictly after: tomorrow
        (D(2026, 11, 11, 12), D(2026, 11, 12, 3)),
        (
            D(2026, 11, 12, 3, 0),
            D(2026, 12, 5, 3),
        ),  # past the 12th: next month
        (D(2026, 12, 31, 23), D(2027, 1, 5, 3)),  # across the year
        (D(2027, 1, 20), D(2027, 2, 5, 3)),
    ],
)
def test_next_inventory_is_daily_on_days_5_to_12(now, expected):
    assert next_inventory(now) == expected


def test_inventory_fires_on_exactly_eight_days_a_month():
    day, hits = D(2026, 11, 1), []
    while day < D(2026, 12, 1):
        nxt = next_inventory(day)
        if nxt.month == 11 and (not hits or hits[-1] != nxt):
            hits.append(nxt)
        day = nxt
    assert [h.day for h in hits] == [5, 6, 7, 8, 9, 10, 11, 12]


class Clock:
    """Drives Scheduler.run: `wait` jumps the clock to the next event instead
    of sleeping."""

    def __init__(self, start, max_waits):
        self.t, self.waits, self.max, self.stop = (
            start,
            0,
            max_waits,
            threading.Event(),
        )
        self.slow = ()  # names of job threads that are meant to outlive a tick

    def now(self):
        return self.t

    def wait(self, seconds):
        for th in list(
            getattr(self, "sch", None) and self.sch._threads.values() or []
        ):
            if th.name not in self.slow:
                th.join(
                    5
                    # let the job that just fired read the clock before it
                    # moves
                    # (else a slow thread sees a later time)
                )
        self.waits += 1
        if self.waits > self.max:
            self.stop.set()
            return True
        self.t += timedelta(seconds=seconds)
        return False


def run_scheduler(start, waits, initial=False):
    calls = []
    clock = Clock(start, waits)
    sch = Scheduler(
        lambda: calls.append(("sync", clock.t)),
        lambda: calls.append(("inv", clock.t)),
        clock.now,
    )
    clock.sch = sch
    sch.run(clock.stop, initial_sync=initial, wait=clock.wait)
    for t in list(sch._threads.values()):
        t.join(5)
    return sorted(calls, key=lambda c: (c[1], c[0]))


def test_runs_every_hour_on_the_hour():
    calls = run_scheduler(
        D(2026, 11, 10, 9, 20), waits=4
    )  # 4 ticks: 10:00, 11:00, 12:00, 13:00
    assert calls == [("sync", D(2026, 11, 10, h)) for h in (10, 11, 12, 13)]


def test_initial_sync_runs_at_startup():
    calls = run_scheduler(D(2026, 11, 10, 9, 20), waits=1, initial=True)
    assert calls == [
        ("sync", D(2026, 11, 10, 9, 20)),
        ("sync", D(2026, 11, 10, 10)),
    ]


def test_inventory_and_sync_both_fire_at_3am_in_the_window():
    calls = run_scheduler(
        D(2026, 11, 5, 1, 30), waits=3
    )  # 02:00, 03:00 (both), 04:00
    assert (
        ("sync", D(2026, 11, 5, 2)) in calls
        and ("inv", D(2026, 11, 5, 3)) in calls
        and ("sync", D(2026, 11, 5, 3)) in calls
    )
    assert [c for c in calls if c[0] == "inv"] == [
        ("inv", D(2026, 11, 5, 3))
    ]  # once, not twice


def test_inventory_never_fires_outside_the_window():
    calls = run_scheduler(
        D(2026, 11, 13, 0, 30), waits=48
    )  # two days after the window
    assert not [c for c in calls if c[0] == "inv"]


def test_an_early_wakeup_does_not_double_fire():
    """Event.wait can return a few ms before the wall-clock tick. The same tick
    must not be scheduled again:
    after firing 10:00 the next wait must be about an hour (until 11:00), not a
    few milliseconds."""
    t = {"now": D(2026, 11, 10, 9, 59, 59, 990000)}
    stop, calls, asked = threading.Event(), [], []

    def wait(seconds):
        asked.append(seconds)
        if len(asked) == 1:
            t["now"] = D(
                2026, 11, 10, 9, 59, 59, 995000
            )  # woke up 5 ms EARLY: the clock hasn't reached 10:00:00
            return False
        stop.set()
        return True

    sch = Scheduler(lambda: calls.append(1), lambda: None, lambda: t["now"])
    sch.run(stop, initial_sync=False, wait=wait)
    for th in sch._threads.values():
        th.join(5)
    assert len(calls) == 1
    assert asked[0] == pytest.approx(
        0.01, abs=0.001
    )  # first wait: until 10:00:00
    assert (
        asked[1] > 3500
    )  # the second wait is until 11:00, not another instant wake-up at 10:00


def test_a_job_still_running_is_skipped_not_stacked():
    gate, started = threading.Event(), []

    def slow():
        started.append(1)
        gate.wait(5)

    clock = Clock(D(2026, 11, 10, 9, 20), 3)
    sch = Scheduler(slow, lambda: None, clock.now)
    sch.run(clock.stop, initial_sync=False, wait=lambda s: clock.wait(s))
    assert (
        len(started) == 1
    )  # ticks 2 and 3 were skipped while the first run was in flight
    gate.set()
    for t in sch._threads.values():
        t.join(5)


def test_a_slow_inventory_does_not_delay_the_hourly_sync():
    gate, order = threading.Event(), []
    clock = Clock(D(2026, 11, 5, 2, 30), 3)  # 03:00 (both), 04:00, 05:00
    sch = Scheduler(
        lambda: order.append("sync"),
        lambda: (order.append("inv-start"), gate.wait(5)),
        clock.now,
    )
    clock.sch, clock.slow = sch, ("jobmcp-inventory",)
    sch.run(clock.stop, initial_sync=False, wait=clock.wait)
    sch._threads["sync"].join(5)
    assert (
        order.count("sync") >= 3 and "inv-start" in order
    )  # the syncs ran while the inventory was still going
    gate.set()
    for t in sch._threads.values():
        t.join(5)


def test_default_jobs_call_the_real_services(monkeypatch):
    from jobmcp import inventory
    from jobmcp.services import sync
    from jobmcp.services import boards

    seen = {}
    monkeypatch.setattr(
        boards, "enable_discovered", lambda s: seen.setdefault("enable", True)
    )
    monkeypatch.setattr(
        sync, "run_due", lambda *a, **k: seen.setdefault("sync", (a, k)) and []
    )
    monkeypatch.setattr(
        inventory,
        "refresh",
        lambda *a, **k: (
            seen.setdefault("inv", (a, k)) and ("up_to_date", None)
        ),
    )
    from jobmcp import health

    monkeypatch.setattr(
        health, "snapshot_vendors", lambda: seen.setdefault("snapshot", True)
    )
    do_sync, do_inv = scheduler.default_jobs()
    do_sync()
    do_inv()
    assert seen["sync"][1] == {
        "slack_min": 10,
        "until_empty": True,
    }  # the same flags as the cron job
    assert (
        seen["enable"] and seen["sync"][0][0] > 1
    )  # leftover 'discovered' boards promoted; a pool of workers
    assert seen["snapshot"]  # the hourly sync records today's counts
    assert seen["inv"][0][:2] == (None, 3)


# ── container start-up commands ─────────────────────────────────────────
pytestmark_db = pytest.mark.usefixtures("fresh_db")


@pytest.mark.usefixtures("fresh_db")
class TestStartupCommands:
    def run_cli(self, monkeypatch, *argv):
        monkeypatch.setattr(
            db, "init", lambda url=None: db.engine()
        )  # keep the test database
        return cli.main(list(argv))

    def test_ensure_admin_generates_a_password_once_and_keeps_existing(
        self, monkeypatch, capsys
    ):
        monkeypatch.delenv("JOBMCP_ADMIN_PASSWORD", raising=False)
        self.run_cli(
            monkeypatch, "ensure-admin", "--email", "Boss@Example.com"
        )
        out = capsys.readouterr().out
        assert (
            "created admin boss@example.com" in out
            and "generated password" in out
        )
        pw = out.split("password (shown once, change it in your portal): ")[
            1
        ].split()[0]
        from jobmcp.services import auth

        with db.session() as s:
            u = s.scalar(select(User).where(User.email == "boss@example.com"))
            assert (
                u.role == "admin" and u.email_verified_at is not None
            )  # able to sign in straight away
            assert auth.authenticate(s, "boss@example.com", pw)
        self.run_cli(
            monkeypatch, "ensure-admin", "--email", "boss@example.com"
        )  # a restart must not reset the password
        assert "already exists" in capsys.readouterr().out
        with db.session() as s:
            assert auth.authenticate(s, "boss@example.com", pw)

    def test_ensure_admin_uses_a_given_password_and_skips_without_an_email(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("JOBMCP_ADMIN_PASSWORD", "chosen-password-123")
        self.run_cli(monkeypatch, "ensure-admin", "--email", "a@x.io")
        assert (
            "generated" not in capsys.readouterr().out
        )  # a password you chose is never echoed back
        monkeypatch.delenv("JOBMCP_ADMIN_EMAIL", raising=False)
        self.run_cli(monkeypatch, "ensure-admin")
        assert "not creating an admin" in capsys.readouterr().out

    def test_wait_db_returns_when_ready_and_gives_up_otherwise(
        self, monkeypatch, capsys
    ):
        self.run_cli(monkeypatch, "wait-db", "--schema", "--timeout", "10")
        assert "schema current" in capsys.readouterr().out
        with db.engine().begin() as c:
            c.exec_driver_sql("DROP TABLE email_tokens")
        with pytest.raises(SystemExit) as e:
            self.run_cli(monkeypatch, "wait-db", "--schema", "--timeout", "3")
        assert "gave up" in str(e.value) and "schema" in str(e.value)

    def test_schema_readiness(self):
        from jobmcp import migrations

        assert migrations.is_current()
        with db.engine().begin() as c:
            c.exec_driver_sql("ALTER TABLE boards DROP COLUMN source")
        assert not migrations.is_current()
        migrations.ensure_schema()
        assert migrations.is_current()


def test_canary_runs_daily_at_0530_alongside_the_other_jobs():
    from jobmcp.scheduler import next_daily

    assert next_daily(D(2026, 11, 10, 5, 0), 5, 30) == D(2026, 11, 10, 5, 30)
    assert next_daily(D(2026, 11, 10, 5, 30), 5, 30) == D(2026, 11, 11, 5, 30)
    calls = []
    clock = Clock(D(2026, 11, 10, 4, 10), 3)  # 05:00 sync, 05:30 canary, 06:00
    sch = Scheduler(
        lambda: calls.append(("sync", clock.t)),
        lambda: None,
        clock.now,
        canary_fn=lambda: calls.append(("canary", clock.t)),
    )
    clock.sch = sch
    sch.run(clock.stop, initial_sync=False, wait=clock.wait)
    for th in sch._threads.values():
        th.join(5)
    assert ("canary", D(2026, 11, 10, 5, 30)) in calls
    assert [c for c in calls if c[0] == "canary"] == [
        ("canary", D(2026, 11, 10, 5, 30))
    ]
