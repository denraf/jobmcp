"""The admin's 'Refresh from Common Crawl' button, the board counters, and the
cross-process refresh lock."""

import threading
import time

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import select

from livejobs import db, inventory, metrics
from livejobs.api import app
from livejobs.orm import InventoryRun, utcnow
from livejobs.services import boards

from .helpers import mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")
PW = "correct-horse-battery"
CRAWL = "CC-MAIN-2026-43"


@pytest.fixture()
def c():
    return TestClient(app)


@pytest.fixture()
def admin(c):
    mk_user("admin@x.io", "admin")
    return {
        "Authorization": "Bearer "
        + c.post(
            "/auth/login", json={"email": "admin@x.io", "password": PW}
        ).json()["token"]
    }


class FakeCrawl:
    """index.commoncrawl.org, with an optional gate so a test can look at the
    system mid-run."""

    def __init__(self, pages=2, gate=None, fail_pages=()):
        self.pages, self.gate, self.fail_pages, self.calls = (
            pages,
            gate,
            set(fail_pages),
            0,
        )

    def fetch(self, crawl, url, match, page):
        self.calls += 1
        if self.gate is not None and self.calls > 2:
            self.gate.wait(20)
        if (url, page) in self.fail_pages:
            raise RuntimeError("504 Gateway Time-out")
        return [f"https://{url}/co{page}a/jobs/1", f"https://{url}/co{page}b"]


@pytest.fixture()
def crawl(monkeypatch):
    fake = FakeCrawl()

    def install(f):
        monkeypatch.setattr(inventory, "list_crawls", lambda n: [CRAWL])
        monkeypatch.setattr(
            inventory, "num_pages", lambda cr, url, match: f.pages
        )
        monkeypatch.setattr(inventory, "fetch_page", f.fetch)
        return f

    install(fake)
    fake.install = install
    return fake


def wait_for(c, admin, done, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        inv = c.get("/admin/inventory", headers=admin).json()
        if done(inv):
            return inv
        time.sleep(0.1)
    raise AssertionError("timed out waiting for the refresh")


# ── counters ────────────────────────────────────────────────────────────
def test_board_counters(c, admin):
    with db.session() as s:
        boards.upsert_discovered(s, "lever", {"a": 5, "b": 3, "c": 1})
        boards.add_board(s, "greenhouse", "mine", enable=True)
        boards.add_board(s, "greenhouse", "off", enable=False)
        boards.update_board(
            s,
            boards.add_board(s, "ashby", "Gone", enable=True).id,
            status="disabled",
        )
    got = c.get("/admin/boards/counts", headers=admin).json()
    # 3 found by the crawl (syncing from the start) + 3 added by hand: one
    # syncing, one never enabled, one disabled
    assert got == {
        "total": 6,
        "enabled": 4,
        "discovered": 1,
        "disabled": 1,
        "manual": 3,
        "crawl": 3,
    }
    with db.session() as s:
        boards.bulk_set_status(s, "lever", "enabled", "disabled", limit=2)
    got = c.get("/admin/boards/counts", headers=admin).json()
    assert got["enabled"] == 2 and got["disabled"] == 3


def test_counters_on_an_empty_system(c, admin):
    assert c.get("/admin/boards/counts", headers=admin).json() == {
        "total": 0,
        "enabled": 0,
        "discovered": 0,
        "disabled": 0,
        "manual": 0,
        "crawl": 0,
    }


def test_counter_and_trigger_endpoints_are_admin_only(c, crawl):
    mk_user("u@x.io")
    user = {
        "Authorization": "Bearer "
        + c.post(
            "/auth/login", json={"email": "u@x.io", "password": PW}
        ).json()["token"]
    }
    for method, path in [
        ("get", "/admin/boards/counts"),
        ("post", "/admin/inventory/refresh"),
        ("get", "/admin/inventory"),
    ]:
        assert getattr(c, method)(path).status_code == 401, path
        assert getattr(c, method)(path, headers=user).status_code == 403, path
    assert (
        crawl.calls == 0
    )  # a normal user cannot make the server hit Common Crawl
    with db.session() as s:
        assert s.scalar(select(InventoryRun)) is None


# ── the button ──────────────────────────────────────────────────────────
def test_button_starts_a_background_refresh_and_reports_progress(
    c, admin, crawl
):
    r = c.post("/admin/inventory/refresh", headers=admin)
    assert (
        r.status_code == 202
        and r.json()["status"] == "started"
        and r.json()["crawl"] == CRAWL
    )
    inv = wait_for(
        c,
        admin,
        lambda i: (
            i["running"] is None
            and i["runs"]
            and i["runs"][0]["status"] == "complete"
        ),
    )
    run = inv["runs"][0]
    assert run["trigger"] == "web" and run["crawl"] == CRAWL
    assert (
        run["pages_fetched"] == run["pages_total"] > 0
        and run["pages_failed"] == 0
    )
    counts = c.get("/admin/boards/counts", headers=admin).json()
    assert (
        run["boards_new"] > 4
        and run["boards_new"] == counts["crawl"] == counts["total"]
    )  # what it found = what the counter shows
    assert (
        counts["enabled"] == counts["total"] and counts["discovered"] == 0
    )  # all of them syncing
    assert inv["last_crawl"] == CRAWL and inv["last_complete"]


def test_progress_is_visible_while_it_runs(c, admin, crawl):
    gate = threading.Event()
    crawl.install(FakeCrawl(gate=gate))
    assert c.post("/admin/inventory/refresh", headers=admin).status_code == 202
    inv = wait_for(
        c, admin, lambda i: i["running"] and i["running"]["pages_fetched"] >= 1
    )
    assert (
        inv["running"]["status"] == "running"
        and inv["running"]["pages_total"] >= inv["running"]["pages_fetched"]
    )
    assert (
        inv["runs"][0]["status"] == "running"
        and not inv["runs"][0]["finished_at"]
    )
    assert (
        inv["running"]["boards_new"] > 0
        # found so far, reported while it runs (and kept if the run is
        # interrupted)
    )
    metrics.collector.reset()
    from livejobs.site import app as site_app

    scrape = TestClient(site_app, client=("127.0.0.1", 1)).get("/metrics").text
    assert (
        "livejobs_inventory_running 1.0" in scrape
    )  # operators can see it too
    gate.set()
    wait_for(c, admin, lambda i: i["running"] is None)
    metrics.collector.reset()
    assert (
        "livejobs_inventory_running 0.0"
        in TestClient(site_app, client=("127.0.0.1", 1)).get("/metrics").text
    )


def test_a_second_click_while_running_is_refused(c, admin, crawl):
    gate = threading.Event()
    crawl.install(FakeCrawl(gate=gate))
    assert c.post("/admin/inventory/refresh", headers=admin).status_code == 202
    wait_for(c, admin, lambda i: i["running"])
    again = c.post("/admin/inventory/refresh", headers=admin)
    assert (
        again.status_code == 409
        and "already running" in again.json()["detail"]
    )
    gate.set()
    wait_for(c, admin, lambda i: i["running"] is None)
    with db.session() as s:
        assert (
            len(list(s.scalars(select(InventoryRun)))) == 1
        )  # the refused click started nothing


def test_clicking_when_the_newest_crawl_is_already_ingested_says_so(
    c, admin, crawl
):
    assert c.post("/admin/inventory/refresh", headers=admin).status_code == 202
    wait_for(
        c, admin, lambda i: i["runs"] and i["runs"][0]["status"] == "complete"
    )
    calls = crawl.calls
    r = c.post("/admin/inventory/refresh", headers=admin)
    assert (
        r.status_code == 200
        and r.json()["status"] == "up_to_date"
        and CRAWL in r.json()["message"]
    )
    assert crawl.calls == calls  # nothing was fetched
    with db.session() as s:
        assert len(list(s.scalars(select(InventoryRun)))) == 1


def test_common_crawl_being_unreachable_is_reported_and_recorded(
    c, admin, monkeypatch
):
    def down(n):
        raise requests.ConnectionError("index.commoncrawl.org unreachable")

    monkeypatch.setattr(inventory, "list_crawls", down)
    r = c.post("/admin/inventory/refresh", headers=admin)
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]
    with db.session() as s:
        run = s.scalar(select(InventoryRun))
        assert run.status == "failed" and run.trigger == "web"
    free = inventory.RefreshLock()
    assert free.acquire()  # no lock was leaked by the failed attempt
    free.release()


def test_failed_pages_end_incomplete_and_the_button_resumes(c, admin, crawl):
    crawl.install(FakeCrawl(fail_pages={("boards.greenhouse.io", 1)}))
    c.post("/admin/inventory/refresh", headers=admin)
    inv = wait_for(c, admin, lambda i: i["running"] is None and i["runs"])
    assert (
        inv["runs"][0]["status"] == "partial"
        and inv["runs"][0]["pages_failed"] == 1
    )
    assert (
        inv["last_complete"] is None
    )  # not complete, so the newest crawl is not considered ingested
    crawl.install(FakeCrawl())  # the index server recovers
    assert (
        c.post("/admin/inventory/refresh", headers=admin).status_code == 202
    )  # the button resumes it
    inv = wait_for(
        c,
        admin,
        lambda i: (
            i["running"] is None and i["runs"][0]["status"] == "complete"
        ),
    )
    assert (
        inv["runs"][0]["pages_fetched"] == 1
    )  # only the one page that had failed


# ── the lock, across processes ──────────────────────────────────────────
def test_the_lock_is_shared_with_the_scheduler_and_the_command_line(
    c, admin, crawl
):
    """A MySQL named lock belongs to a connection, so a second RefreshLock is a
    stand-in for another process."""
    other_process = inventory.RefreshLock()
    assert other_process.acquire()
    try:
        assert inventory.refresh(None, 1, log=lambda m: None) == (
            "already_running",
            None,
        )  # scheduler / CLI
        assert (
            c.post("/admin/inventory/refresh", headers=admin).status_code
            == 409
        )  # the button
        assert crawl.calls == 0
    finally:
        other_process.release()
    assert (
        c.post("/admin/inventory/refresh", headers=admin).status_code == 202
    )  # free again
    wait_for(c, admin, lambda i: i["running"] is None)


def test_the_lock_is_released_even_when_a_run_crashes(monkeypatch):
    monkeypatch.setattr(inventory, "list_crawls", lambda n: [CRAWL])

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(inventory, "build", boom)
    status, run = inventory.refresh(None, 1, log=lambda m: None)
    assert status == "failed" and "went away" in run.error
    again = inventory.RefreshLock()
    assert again.acquire()  # not stuck
    again.release()


def test_two_simultaneous_starts_only_one_wins(crawl):
    gate = threading.Event()
    crawl.install(FakeCrawl(gate=gate))
    first = inventory.start_background(log=lambda m: None)
    second = inventory.start_background(log=lambda m: None)
    assert (first["status"], second["status"]) == (
        "started",
        "already_running",
    )
    gate.set()
    first["thread"].join(20)
    with db.session() as s:
        assert len(list(s.scalars(select(InventoryRun)))) == 1


def test_a_run_left_running_by_a_dead_process_is_not_reported_as_running(
    c, admin, crawl
):
    """kill -9 leaves the row saying 'running' (the MySQL lock frees itself).
    The admin page must not show a refresh
    that no longer exists, or its button stays disabled with nothing able to
    re-enable it."""
    with db.session() as s:
        s.add(
            InventoryRun(
                crawl="CC-MAIN-2026-39",
                status="running",
                started_at=utcnow(),
                pages_fetched=7,
                pages_total=16,
                trigger="web",
            )
        )
    inv = c.get("/admin/inventory", headers=admin).json()
    assert (
        inv["running"] is None
    )  # nothing holds the lock, so nothing is running
    stale = inv["runs"][0]
    assert (
        stale["status"] == "failed"
        and "interrupted" in stale["error"]
        and stale["finished_at"]
        and stale["pages_fetched"] == 7
    )
    assert (
        c.post("/admin/inventory/refresh", headers=admin).status_code == 202
    )  # and the button works again
    inv = wait_for(
        c,
        admin,
        lambda i: (
            i["running"] is None and i["runs"][0]["status"] == "complete"
        ),
    )
    assert (
        sum(r["status"] == "failed" for r in inv["runs"]) == 1
    )  # the interrupted one is recorded once, not duplicated


def test_a_live_run_is_never_mistaken_for_a_dead_one(c, admin, crawl):
    gate = threading.Event()
    crawl.install(FakeCrawl(gate=gate))
    c.post("/admin/inventory/refresh", headers=admin)
    wait_for(c, admin, lambda i: i["running"])
    for _ in range(
        3
        # repeated page loads while it runs must not close the run behind its
        # back
    ):
        inv = c.get("/admin/inventory", headers=admin).json()
        assert (
            inv["running"]
            and inv["running"]["status"] == "running"
            and inv["runs"][0]["status"] == "running"
        )
    assert inventory.reconcile_stale() == 0 and inventory.is_running()
    gate.set()
    inv = wait_for(c, admin, lambda i: i["running"] is None)
    assert (
        inv["runs"][0]["status"] == "complete" and not inventory.is_running()
    )


def test_metrics_do_not_report_a_dead_run_as_running(crawl):
    from livejobs.site import app as site_app

    with db.session() as s:
        s.add(
            InventoryRun(
                crawl="CC-MAIN-2026-39",
                status="running",
                started_at=utcnow(),
                trigger="web",
            )
        )
    metrics.collector.reset()
    assert (
        "livejobs_inventory_running 0.0"
        in TestClient(site_app, client=("127.0.0.1", 1)).get("/metrics").text
    )


def test_schema_upgrade_adds_the_progress_columns():
    from sqlalchemy import text

    from livejobs import migrations

    with db.engine().begin() as conn:
        conn.execute(
            text("ALTER TABLE inventory_runs DROP COLUMN pages_total")
        )
        conn.execute(text("ALTER TABLE inventory_runs DROP COLUMN `trigger`"))
    assert not migrations.is_current()
    assert any("pages_total" in x for x in migrations.ensure_schema())
    assert migrations.is_current() and migrations.ensure_schema() == []


def test_the_lock_is_per_database_so_installs_do_not_block_each_other():
    from sqlalchemy import text

    name = inventory.lock_name()
    assert len(name) <= 64 and name.startswith("livejobs:inventory:")
    # another install's lock (a different database name hashes differently)
    # held on the same server
    other = db.engine().connect()
    try:
        assert (
            other.execute(
                text("SELECT GET_LOCK(:n, 0)"),
                {"n": "livejobs:inventory:" + "0" * 24},
            ).scalar()
            == 1
        )
        mine = inventory.RefreshLock()
        assert (
            mine.acquire() and inventory.is_running()
        )  # not blocked by the other install
        mine.release()
        assert (
            not inventory.is_running()
        )  # and the other install's lock does not look like ours
    finally:
        other.execute(
            text("SELECT RELEASE_LOCK(:n)"),
            {"n": "livejobs:inventory:" + "0" * 24},
        )
        other.close()
