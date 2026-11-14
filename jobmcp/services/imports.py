"""Long board imports run in the worker service, so a huge pasted list never
ties up the web service (or its memory or its CPU).

The web request validates the paste, queues a task and answers at once with
its id; the browser polls `get(id)` for progress. The worker (`run_task`)
checks, saves and syncs the boards and writes progress as it goes. If either
service restarts mid-import, the boards already saved stay saved and the admin
can paste the list again (entries already added are reported as 'exists').
One import runs at a time."""

from __future__ import annotations

import logging

from .. import config, db
from . import ServiceError, boards, tasks

log = logging.getLogger("jobmcp.import")

INLINE_MAX = 20  # up to this many lines are done within the request, as before
MAX_ENTRIES = 100_000  # a sanity limit on one paste
# lines that need the admin's attention are kept for display; successes are
# only counted
KEEP_PROBLEMS = 500


def execute(lines, enable, verify, admin_id, workers, fetcher, progress):
    """Check, save and sync every line (see boards.add_many)."""
    with db.session() as s:
        boards.add_many(
            s,
            lines,
            enable,
            verify,
            added_by=admin_id,
            fetcher=fetcher,
            progress=progress,
            workers=workers,
        )


# -- the web service's side --------------------------------------------------
def start(
    entries: list[str], enable: bool, verify: bool, admin_id: int
) -> dict:
    lines = [
        e for e in (x.strip() for x in entries) if e and not e.startswith("#")
    ]
    if len(lines) > MAX_ENTRIES:
        raise ServiceError(f"at most {MAX_ENTRIES:,} lines at a time", 422)
    t = tasks.enqueue(
        "import",
        {
            "lines": lines,
            "enable": enable,
            "verify": verify,
            "admin_id": admin_id,
            "workers": config.IMPORT_WORKERS,
        },
        by=admin_id,
        progress={
            "total": len(lines),
            "done": 0,
            "summary": {},
            "problems": [],
            "problems_truncated": False,
        },
    )
    return public(t)


def public(t: dict) -> dict:
    p = t["progress"]
    start = t["started_at"] or t["created_at"]
    end = t["finished_at"] or tasks.utcnow()
    return {
        "id": t["id"],
        "state": {"queued": "running", "running": "running"}.get(
            t["state"], t["state"]
        ),
        "waiting_for_worker": t["state"] == "queued",
        "total": p.get("total", 0),
        "done": p.get("done", 0),
        "summary": p.get("summary", {}),
        "problems": p.get("problems", []),
        "problems_truncated": p.get("problems_truncated", False),
        "error": t["error"],
        "seconds": max(0, int((end - start).total_seconds())),
    }


def get(job_id: int) -> dict:
    t = tasks.get(job_id)
    if not t or t["kind"] != "import":
        raise ServiceError("no such import", 404)
    return public(t)


def current() -> dict | None:
    t = tasks.latest("import")
    return public(t) if t else None


# -- the worker's side -------------------------------------------------------
def run_task(payload: dict, ctx: tasks.Context, fetcher=None) -> dict:
    """Do an import. Progress is saved after every chunk of boards."""
    log.info(
        "import of %d lines started",
        len(payload["lines"]),
        extra={"admin_id": payload["admin_id"], "task": ctx.task_id},
    )

    def on_progress(rs: list[dict]) -> None:
        st = ctx.state
        summary, problems = (
            dict(st.get("summary", {})),
            list(st.get("problems", [])),
        )
        truncated = st.get("problems_truncated", False)
        for r in rs:
            summary[r["status"]] = summary.get(r["status"], 0) + 1
            if r["status"] not in ("added", "exists", "duplicate_in_list"):
                if len(problems) < KEEP_PROBLEMS:
                    problems.append(r)
                else:
                    truncated = True
        ctx.progress(
            done=min(st.get("total", 0), st.get("done", 0) + len(rs)),
            summary=summary,
            problems=problems,
            problems_truncated=truncated,
        )

    execute(
        payload["lines"],
        payload["enable"],
        payload["verify"],
        payload["admin_id"],
        payload.get("workers") or config.IMPORT_WORKERS,
        fetcher,
        on_progress,
    )
    log.info("import finished: %s", ctx.state.get("summary"))
    return {"summary": ctx.state.get("summary", {})}
