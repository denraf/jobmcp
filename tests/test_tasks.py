"""The web service and the worker service share only the `tasks` table: the web
service queues work and reads progress, the worker does it."""

import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from jobmcp import cli, db
from jobmcp.api import app
from jobmcp.orm import Board, Job, Task, utcnow
from jobmcp.services import ServiceError, imports, sync, tasks
from jobmcp.vendors.base import BoardGone

from .helpers import fj, mk_board, mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")
PW = "correct-horse-battery"


@pytest.fixture()
def c():
    return TestClient(app)


@pytest.fixture()
def admin(c):
    mk_user("admin@x.io", "admin")
    token = c.post(
        "/auth/login", json={"email": "admin@x.io", "password": PW}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def calls(monkeypatch):
    """Every fetch of a board, wherever it happens."""
    seen = []

    def fetch(vendor, board):
        seen.append((vendor, board))
        return [fj("Engineer", board=board, vendor=vendor)]

    monkeypatch.setattr(sync, "default_fetcher", fetch)
    return seen


# ── the queue ───────────────────────────────────────────────────────────
def test_a_one_at_a_time_task_refuses_a_second_until_the_first_ends():
    first = tasks.enqueue("canary")
    assert first["state"] == "queued"
    with pytest.raises(ServiceError) as e:
        tasks.enqueue("canary")
    assert e.value.code == 409 and "already running" in str(e.value)
    me = tasks.worker_name()
    assert tasks.claim(me)["id"] == first["id"]
    with pytest.raises(ServiceError):
        tasks.enqueue("canary")  # running still counts
    tasks.finish(first["id"], "done", {"checked": 0})
    assert tasks.enqueue("canary")["id"] > first["id"]  # the slot is free


def test_kinds_that_may_overlap_do_not_block_each_other(calls):
    a = tasks.enqueue("sync_board", {"board_id": 1})
    b = tasks.enqueue("sync_board", {"board_id": 2})
    assert a["id"] != b["id"]


def test_two_workers_never_claim_the_same_task():
    for _ in range(6):
        tasks.enqueue("sync_boards", {"ids": []})
    won, lock = [], threading.Lock()

    def grab(name):
        while t := tasks.claim(name):
            with lock:
                won.append(t["id"])

    ts = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(won) == sorted(set(won)) and len(won) == 6


def test_the_oldest_task_is_taken_first():
    first = tasks.enqueue("sync_boards", {"ids": []})
    tasks.enqueue("sync_boards", {"ids": []})
    assert tasks.claim("w")["id"] == first["id"]


def test_a_failing_handler_is_recorded_and_frees_the_slot(monkeypatch):
    def boom(payload, ctx):
        raise RuntimeError("the platform exploded")

    monkeypatch.setattr(tasks, "handler_for", lambda kind: boom)
    t = tasks.enqueue("canary")
    assert tasks.run_pending() == 1
    got = tasks.get(t["id"])
    assert got["state"] == "failed" and "exploded" in got["error"]
    assert got["finished_at"]
    tasks.enqueue("canary")  # not stuck


def test_a_task_whose_worker_died_is_failed_and_frees_the_slot():
    t = tasks.enqueue("import", {"lines": []})
    tasks.claim("dead-worker")
    assert tasks.reconcile_stale() == 0  # it has just reported
    with db.session() as s:
        s.execute(
            update(Task)
            .where(Task.id == t["id"])
            .values(heartbeat_at=utcnow() - timedelta(minutes=30))
        )
    assert tasks.reconcile_stale() == 1
    got = tasks.get(t["id"])
    assert got["state"] == "failed" and "worker stopped" in got["error"]
    tasks.enqueue("import", {"lines": []})  # the admin can ask again


def test_a_second_request_after_a_crash_reclaims_the_slot_itself():
    t = tasks.enqueue("canary")
    tasks.claim("dead-worker")
    with db.session() as s:
        s.execute(
            update(Task)
            .where(Task.id == t["id"])
            .values(heartbeat_at=utcnow() - timedelta(hours=1))
        )
    assert tasks.enqueue("canary")["state"] == "queued"


def test_progress_is_saved_for_the_web_service_to_read():
    t = tasks.enqueue("sync_all", progress={"total": 5})
    ctx = tasks.Context(t["id"], t["progress"])
    ctx.progress(done=2)
    ctx.progress(ok=2)
    assert tasks.get(t["id"])["progress"] == {"total": 5, "done": 2, "ok": 2}


def test_finished_rows_are_pruned(monkeypatch):
    monkeypatch.setattr(tasks, "KEEP_FINISHED", 3)
    for _ in range(6):
        t = tasks.enqueue("sync_boards", {"ids": []})
        tasks.claim("w")
        tasks.finish(t["id"], "done")
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == 3


def test_the_worker_loop_runs_tasks_and_stops_cleanly(calls):
    bid = mk_board("lever", "acme")
    stop = threading.Event()
    th = threading.Thread(target=tasks.run_forever, args=(stop, 2, 0.05))
    th.start()
    try:
        t = sync.request_board(bid)
        for _ in range(100):
            got = tasks.get(t["id"])
            if got["state"] in ("done", "failed"):
                break
            stop.wait(0.05)
    finally:
        stop.set()
        th.join(20)
    assert not th.is_alive()
    assert got["state"] == "done" and got["result"]["ok"] is True
    assert calls == [("lever", "acme")]


def test_the_tasks_command_runs_what_is_queued_and_exits(calls, capsys):
    sync.request_board(mk_board("lever", "acme"))
    cli.main(["tasks", "--once"])
    assert "1 task(s) run" in capsys.readouterr().out
    assert calls == [("lever", "acme")]


# ── the web service queues, it does not work ────────────────────────────
def test_sync_all_is_queued_by_the_web_service_and_done_by_the_worker(
    c, admin, calls
):
    for i in range(5):
        mk_board("lever", f"b{i}")
    r = c.post("/admin/boards/sync-all", headers=admin)
    assert r.status_code == 202 and r.json()["total"] == 5
    assert r.json()["state"] == "running" and r.json()["waiting_for_worker"]
    assert calls == []  # the web request fetched nothing
    st = c.get("/admin/boards/sync-all", headers=admin).json()
    assert st["state"] == "running" and st["done"] == 0
    tasks.run_pending()  # the worker
    st = c.get("/admin/boards/sync-all", headers=admin).json()
    assert (st["state"], st["done"], st["ok"], st["jobs"]) == ("done", 5, 5, 5)
    assert not st["waiting_for_worker"]
    assert len(calls) == 5


def test_a_long_import_is_queued_and_the_web_service_saves_nothing(
    c, admin, calls
):
    lines = [f"lever:b{i}" for i in range(30)]
    r = c.post("/admin/boards/import", json={"entries": lines}, headers=admin)
    assert r.status_code == 202 and r.json()["total"] == 30
    job = r.json()
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Board)) == 0
    assert calls == []
    again = c.post(
        "/admin/boards/import", json={"entries": lines}, headers=admin
    )
    assert (
        again.status_code == 409
        and "already running" in again.json()["detail"]
    )
    tasks.run_pending()
    done = c.get(f"/admin/boards/import/{job['id']}", headers=admin).json()
    assert done["state"] == "done" and done["done"] == 30
    assert done["summary"] == {"added": 30}
    assert (
        c.get("/admin/boards/import/current", headers=admin).json()["id"]
        == (job["id"])
    )
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 30


def test_an_import_that_fails_part_way_keeps_its_progress(
    c, admin, monkeypatch
):
    def boom(vendor, board):
        raise RuntimeError("database went away")

    monkeypatch.setattr(sync, "default_fetcher", boom)
    monkeypatch.setattr(
        imports, "execute", lambda *a, **k: (_ for _ in ()).throw(boom(0, 0))
    )
    r = c.post(
        "/admin/boards/import",
        json={"entries": [f"lever:x{i}" for i in range(25)]},
        headers=admin,
    )
    tasks.run_pending()
    got = c.get(f"/admin/boards/import/{r.json()['id']}", headers=admin).json()
    assert got["state"] == "failed" and "went away" in got["error"]
    assert got["total"] == 25


def test_a_short_import_answers_inline_but_leaves_the_sync_to_the_worker(
    c, admin, calls
):
    r = c.post(
        "/admin/boards/import",
        json={"entries": ["lever:acme", "lever:other"], "verify": False},
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["summary"] == {"added": 2}
    assert calls == []  # not verified, not synced: the request did no fetching
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0
    tasks.run_pending()
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 2


def test_manual_sync_answers_with_a_task_the_page_can_poll(c, admin, calls):
    bid = mk_board("lever", "acme")
    r = c.post(f"/admin/boards/{bid}/sync", headers=admin)
    assert r.status_code == 202 and r.json()["state"] == "queued"
    assert calls == []
    tasks.run_pending()
    got = c.get(f"/admin/tasks/{r.json()['id']}", headers=admin).json()
    assert got["state"] == "done"
    assert (
        got["result"]["ok"],
        got["result"]["jobs"],
        got["result"]["new"],
    ) == (
        True,
        1,
        1,
    )
    assert c.post("/admin/boards/99999/sync", headers=admin).status_code == 404


def test_task_endpoints_are_admin_only(c):
    mk_user("u@x.io", "user")
    token = c.post(
        "/auth/login", json={"email": "u@x.io", "password": PW}
    ).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    t = tasks.enqueue("sync_boards", {"ids": []})
    assert c.get(f"/admin/tasks/{t['id']}").status_code == 401
    assert c.get(f"/admin/tasks/{t['id']}", headers=h).status_code == 403


def test_a_board_that_is_gone_is_a_finished_task_not_a_crashed_one(
    c, admin, monkeypatch
):
    def gone(v, b):
        raise BoardGone(b)

    monkeypatch.setattr(sync, "default_fetcher", gone)
    bid = mk_board("lever", "acme")
    r = c.post(f"/admin/boards/{bid}/sync", headers=admin)
    tasks.run_pending()
    got = tasks.get(r.json()["id"])
    assert got["state"] == "done" and got["result"]["ok"] is False
