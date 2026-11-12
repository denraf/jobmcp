"""Long board imports run in the background, so a huge pasted list never ties
up a web request (or its memory).

The web request validates and returns at once with an id; the browser polls
`get(id)` for progress. State is in this
process only: if the web service restarts mid-import, the boards already saved
stay saved and the admin can paste the
list again (entries already added are reported as 'exists'). One import runs at
a time."""

from __future__ import annotations

import logging
import threading
import time
import uuid

from .. import config, db
from . import ServiceError, boards

log = logging.getLogger("livejobs.import")

INLINE_MAX = 20  # up to this many lines are done within the request, as before
MAX_ENTRIES = 100_000  # a sanity limit on one paste
# lines that need the admin's attention are kept for display; successes are
# only counted
KEEP_PROBLEMS = 500
_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _running() -> dict | None:
    return next((j for j in _jobs.values() if j["state"] == "running"), None)


def start(
    entries: list[str],
    enable: bool,
    verify: bool,
    admin_id: int,
    fetcher=None,
    workers: int | None = None,
) -> dict:
    lines = [
        e for e in (x.strip() for x in entries) if e and not e.startswith("#")
    ]
    if len(lines) > MAX_ENTRIES:
        raise ServiceError(f"at most {MAX_ENTRIES:,} lines at a time", 422)
    with _lock:
        if _running():
            raise ServiceError(
                "An import is already running. Its progress is shown above; "
                "wait for it to finish.",
                409,
            )
        job = {
            "id": uuid.uuid4().hex[:12],
            "state": "running",
            "total": len(lines),
            "done": 0,
            "summary": {},
            "problems": [],
            "problems_truncated": False,
            "error": None,
            "started": time.time(),
            "finished": None,
        }
        _jobs[job["id"]] = job
        for old in sorted(_jobs.values(), key=lambda j: j["started"])[
            :-10
        ]:  # remember only the latest few
            _jobs.pop(old["id"], None)

    def on_progress(rs: list[dict]) -> None:
        with _lock:
            job["done"] = min(job["total"], job["done"] + len(rs))
            for r in rs:
                job["summary"][r["status"]] = (
                    job["summary"].get(r["status"], 0) + 1
                )
                if r["status"] not in ("added", "exists", "duplicate_in_list"):
                    if len(job["problems"]) < KEEP_PROBLEMS:
                        job["problems"].append(r)
                    else:
                        job["problems_truncated"] = True

    def run() -> None:
        log.info(
            "import of %d lines started",
            len(lines),
            extra={"admin_id": admin_id, "job": job["id"]},
        )
        kw = {"workers": workers or config.IMPORT_WORKERS}
        try:
            with db.session() as s:
                boards.add_many(
                    s,
                    lines,
                    enable,
                    verify,
                    added_by=admin_id,
                    fetcher=fetcher,
                    progress=on_progress,
                    **kw,
                )
            job["state"] = "done"
        except Exception as e:  # keep what was saved; say what stopped it
            log.exception("import failed", extra={"job": job["id"]})
            job["state"], job["error"] = (
                "failed",
                f"{type(e).__name__}: {e}"[:300],
            )
        job["finished"] = time.time()
        log.info(
            "import finished: %s",
            job["summary"],
            extra={
                "job": job["id"],
                "state": job["state"],
                "seconds": int(job["finished"] - job["started"]),
            },
        )

    job["thread"] = t = threading.Thread(
        target=run, name="livejobs-import", daemon=True
    )
    t.start()
    return public(job)


def public(job: dict) -> dict:
    return {
        k: v
        for k, v in job.items()
        if k not in ("thread", "started", "finished")
    } | {"seconds": int((job["finished"] or time.time()) - job["started"])}


def get(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise ServiceError(
                "no such import (the web service may have restarted; boards "
                "already saved are kept)",
                404,
            )
        return public(job)


def current() -> dict | None:
    with _lock:
        j = _running() or (
            max(_jobs.values(), key=lambda j: j["started"]) if _jobs else None
        )
        return public(j) if j else None
