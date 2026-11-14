"""The queue between the web service and the worker service.

The web service never fetches a job board, parses postings or stores them in
bulk: it inserts a row here (`enqueue`) and reads progress back (`get`,
`latest`). The worker service (`jobmcp tasks`) claims rows, does the work and
writes progress as it goes. They share nothing but the database, so they can
run in different containers, and a restart of either loses nothing that was
already saved.

A task is queued -> running -> done | failed. Some kinds run one at a time
(`ONE_AT_A_TIME`): while one is queued or running its row holds the unique
`active_key`, so a second request is refused (409) even when two web workers
get the click at the same moment.
"""

from __future__ import annotations

import importlib
import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .. import config, db
from ..orm import Task, utcnow
from . import ServiceError

log = logging.getLogger("jobmcp.tasks")

# kind -> (module, function). Imported when a task runs: the worker needs all
# of them, the web service none.
HANDLERS: dict[str, tuple[str, str]] = {
    "import": ("jobmcp.services.imports", "run_task"),
    "sync_all": ("jobmcp.services.sync", "run_all_task"),
    "sync_board": ("jobmcp.services.sync", "run_board_task"),
    "sync_boards": ("jobmcp.services.sync", "run_boards_task"),
    "canary": ("jobmcp.canary", "run_task"),
    "inventory": ("jobmcp.inventory", "run_task"),
}
ONE_AT_A_TIME = frozenset({"import", "sync_all", "canary", "inventory"})
BUSY = {
    "import": (
        "An import is already running. Its progress is shown above; wait for "
        "it to finish."
    ),
    "sync_all": (
        "A sync of all boards is already running. Its progress is shown above."
    ),
    "canary": "A platform check is already running.",
    "inventory": (
        "A Common Crawl refresh is already running. Its progress is shown "
        "above."
    ),
}
HEARTBEAT_SECONDS = 20
KEEP_FINISHED = 200  # finished rows kept per kind; older ones are removed

Handler = Callable[[dict, "Context"], "dict | None"]


def as_dict(t: Task, payload: bool = False) -> dict:
    out = {
        "id": t.id,
        "kind": t.kind,
        "state": t.state,
        "progress": t.progress or {},
        "result": t.result,
        "error": t.error,
        "created_at": t.created_at,
        "started_at": t.started_at,
        "finished_at": t.finished_at,
    }
    if payload:
        out["payload"] = t.payload
    return out


def stale_after() -> timedelta:
    return timedelta(minutes=config.TASK_STALE_MIN)


def enqueue(
    kind: str,
    payload: dict | None = None,
    *,
    by: int | None = None,
    progress: dict | None = None,
    busy: str | None = None,
) -> dict:
    """Ask the worker to do something. Returns the new task. For a kind that
    runs one at a time, raises ServiceError(409) while one is already queued
    or running."""
    if kind not in HANDLERS:
        raise ValueError(f"unknown task kind {kind!r}")
    single = kind in ONE_AT_A_TIME
    for attempt in (1, 2):
        try:
            with db.session() as s:
                t = Task(
                    kind=kind,
                    payload=payload,
                    progress=progress,
                    requested_by=by,
                    active_key=kind if single else None,
                )
                s.add(t)
                s.flush()
                out = as_dict(t)
            log.info("task queued", extra={"task": out["id"], "kind": kind})
            return out
        except IntegrityError:
            # either a live task of this kind, or one whose worker died: end
            # the dead ones and look again
            if attempt == 1 and reconcile_stale():
                continue
            raise ServiceError(busy or BUSY[kind], 409) from None
    raise AssertionError("unreachable")


def get(task_id: int) -> dict | None:
    with db.session() as s:
        t = s.get(Task, task_id)
        return as_dict(t) if t else None


def latest(kind: str) -> dict | None:
    """The running one if there is one, otherwise the most recent."""
    with db.session() as s:
        t = s.scalar(
            select(Task)
            .where(Task.kind == kind)
            .order_by(
                (Task.state.in_(("queued", "running"))).desc(), Task.id.desc()
            )
            .limit(1)
        )
        return as_dict(t) if t else None


def set_progress(task_id: int, progress: dict) -> None:
    with db.session() as s:
        s.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(progress=progress, heartbeat_at=utcnow())
        )


def claim(worker: str) -> dict | None:
    """Take the oldest queued task, or None. Safe with several workers: the
    update only succeeds for the one that finds it still queued."""
    with db.session() as s:
        ids = list(
            s.scalars(
                select(Task.id)
                .where(Task.state == "queued")
                .order_by(Task.id)
                .limit(5)
            )
        )
        for tid in ids:
            now = utcnow()
            won = s.execute(
                update(Task)
                .where(Task.id == tid, Task.state == "queued")
                .values(
                    state="running",
                    worker=worker,
                    started_at=now,
                    heartbeat_at=now,
                )
            )
            if won.rowcount == 1:
                s.flush()
                t = s.get(Task, tid)
                s.refresh(t)
                return as_dict(t, payload=True)
    return None


def finish(
    task_id: int,
    state: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    with db.session() as s:
        s.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                state=state,
                result=result,
                error=(error or None) and error[:300],
                finished_at=utcnow(),
                active_key=None,
            )
        )
        kind = s.scalar(select(Task.kind).where(Task.id == task_id))
        if kind:  # keep the table small
            old = list(
                s.scalars(
                    select(Task.id)
                    .where(
                        Task.kind == kind,
                        Task.state.in_(("done", "failed")),
                    )
                    .order_by(Task.id.desc())
                    .offset(KEEP_FINISHED)
                )
            )
            if old:
                s.query(Task).filter(Task.id.in_(old)).delete(
                    synchronize_session=False
                )


def reconcile_stale() -> int:
    """End tasks whose worker stopped heartbeating (killed, container gone):
    they are marked failed, which also frees the one-at-a-time slot. The work
    they did stays done; the admin can ask again. Returns how many."""
    cutoff = utcnow() - stale_after()
    with db.session() as s:
        res = s.execute(
            update(Task)
            .where(Task.state == "running", Task.heartbeat_at < cutoff)
            .values(
                state="failed",
                error="the worker stopped before this finished",
                finished_at=utcnow(),
                active_key=None,
            )
        )
        # queued for longer than that with no worker at all: leave it queued
        # (a worker coming up will take it), but do not hold the slot forever
        return res.rowcount or 0


class Context:
    """What a handler gets besides its payload."""

    def __init__(self, task_id: int, progress: dict | None = None):
        self.task_id = task_id
        self.state: dict = dict(progress or {})
        self._lock = threading.Lock()

    def progress(self, **changes: Any) -> None:
        """Merge `changes` into the task's progress and save it."""
        with self._lock:
            self.state.update(changes)
            set_progress(self.task_id, dict(self.state))

    def save(self) -> None:
        with self._lock:
            set_progress(self.task_id, dict(self.state))


def handler_for(kind: str) -> Handler:
    module, name = HANDLERS[kind]
    return getattr(importlib.import_module(module), name)


def run_task(task: dict) -> None:
    """Do one claimed task and record how it ended."""
    ctx = Context(task["id"], task.get("progress"))
    log.info("task started", extra={"task": task["id"], "kind": task["kind"]})
    try:
        result = handler_for(task["kind"])(task.get("payload") or {}, ctx)
        finish(task["id"], "done", result)
        log.info(
            "task finished", extra={"task": task["id"], "kind": task["kind"]}
        )
    except Exception as e:
        log.exception(
            "task failed", extra={"task": task["id"], "kind": task["kind"]}
        )
        finish(task["id"], "failed", None, f"{type(e).__name__}: {e}")


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:80]


def run_pending(limit: int | None = None) -> int:
    """Run queued tasks one after another in this thread, until none is left.
    For `jobmcp tasks --once` and for tests."""
    n, me = 0, worker_name()
    while limit is None or n < limit:
        t = claim(me)
        if not t:
            return n
        run_task(t)
        n += 1
    return n


def run_forever(
    stop: threading.Event, slots: int | None = None, poll: float = 2.0
) -> None:
    """The worker service's loop: up to `slots` tasks at once (a long import
    does not hold up a platform check), polling for new ones."""
    slots = slots or config.TASK_SLOTS
    me, running = worker_name(), set()
    lock = threading.Lock()
    log.info("task worker started (%d at once, poll %.1fs)", slots, poll)

    def beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            with lock:
                ids = list(running)
            if ids:
                try:
                    with db.session() as s:
                        s.execute(
                            update(Task)
                            .where(Task.id.in_(ids), Task.state == "running")
                            .values(heartbeat_at=utcnow())
                        )
                except Exception:
                    log.exception("heartbeat failed")

    def work(t: dict) -> None:
        try:
            run_task(t)
        finally:
            with lock:
                running.discard(t["id"])

    threading.Thread(target=beat, name="task-heartbeat", daemon=True).start()
    ticks = 0
    with ThreadPoolExecutor(slots, thread_name_prefix="task") as ex:
        while not stop.is_set():
            try:
                if ticks % max(1, int(60 / poll)) == 0:
                    reconcile_stale()
                ticks += 1
                with lock:
                    free = slots - len(running)
                claimed = 0
                for _ in range(max(0, free)):
                    t = claim(me)
                    if not t:
                        break
                    with lock:
                        running.add(t["id"])
                    ex.submit(work, t)
                    claimed += 1
                if claimed:
                    continue
            except Exception:
                log.exception("task loop failed")
            stop.wait(poll)
        log.info("task worker stopping; waiting for running tasks")
