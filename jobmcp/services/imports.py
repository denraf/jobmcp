"""Long board imports run in the background, so a huge pasted list never ties
up a web request (or its memory).

The web request validates and returns at once with an id; the browser polls
`get(id)` for progress. State is in this
process only: if the web service restarts mid-import, the boards already saved
stay saved and the admin can paste the
list again (entries already added are reported as 'exists'). One import runs at
a time."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid

from .. import config, db
from . import ServiceError, boards

log = logging.getLogger("jobmcp.import")

INLINE_MAX = 20  # up to this many lines are done within the request, as before
MAX_ENTRIES = 100_000  # a sanity limit on one paste
# lines that need the admin's attention are kept for display; successes are
# only counted
KEEP_PROBLEMS = 500
_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _running() -> dict | None:
    return next((j for j in _jobs.values() if j["state"] == "running"), None)


PROTOCOL = "@@import "  # a line from the child process that is for us


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


def child_main() -> None:
    """`jobmcp import-run`: read the job on stdin, do it, report progress as
    protocol lines on stdout (everything else on stdout is ordinary logging).
    Runs at a lower priority than the web service."""
    try:
        os.nice(10)
    except OSError:
        pass
    job = json.load(sys.stdin)

    def say(msg: dict) -> None:
        print(PROTOCOL + json.dumps(msg), flush=True)

    try:
        execute(
            job["lines"],
            job["enable"],
            job["verify"],
            job["admin_id"],
            job["workers"],
            None,
            lambda rs: say({"results": rs}),
        )
        say({"state": "done"})
    except Exception as e:
        log.exception("import failed")
        say({"state": "failed", "error": f"{type(e).__name__}: {e}"[:300]})


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

    def finish(state: str, error: str | None = None) -> None:
        job["state"], job["error"] = state, error
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

    def run() -> None:
        log.info(
            "import of %d lines started",
            len(lines),
            extra={"admin_id": admin_id, "job": job["id"]},
        )
        try:
            execute(
                lines,
                enable,
                verify,
                admin_id,
                workers or config.IMPORT_WORKERS,
                fetcher,
                on_progress,
            )
            finish("done")
        except Exception as e:  # keep what was saved; say what stopped it
            log.exception("import failed", extra={"job": job["id"]})
            finish("failed", f"{type(e).__name__}: {e}"[:300])

    def run_in_child() -> None:
        """The same work in its own process: fetching, parsing and storing
        thousands of postings is CPU work, and in this process it would
        compete with every page for the interpreter lock."""
        log.info(
            "import of %d lines started in its own process",
            len(lines),
            extra={"admin_id": admin_id, "job": job["id"]},
        )
        job_in = {
            "lines": lines,
            "enable": enable,
            "verify": verify,
            "admin_id": admin_id,
            "workers": workers or config.IMPORT_WORKERS,
        }
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "jobmcp", "import-run"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            proc.stdin.write(json.dumps(job_in))
            proc.stdin.close()
            result = {"state": "failed", "error": "the import stopped early"}
            for line in proc.stdout:
                if not line.startswith(PROTOCOL):
                    sys.stdout.write(line)  # the child's own log lines
                    continue
                msg = json.loads(line[len(PROTOCOL) :])
                if "results" in msg:
                    on_progress(msg["results"])
                else:
                    result = msg
            proc.wait()
            if proc.returncode and result["state"] == "done":
                result = {
                    "state": "failed",
                    "error": f"exit {proc.returncode}",
                }
            finish(result["state"], result.get("error"))
        except Exception as e:
            log.exception("import failed", extra={"job": job["id"]})
            finish("failed", f"{type(e).__name__}: {e}"[:300])

    target = run if fetcher or config.IMPORT_IN_PROCESS else run_in_child
    job["thread"] = t = threading.Thread(
        target=target, name="jobmcp-import", daemon=True
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
