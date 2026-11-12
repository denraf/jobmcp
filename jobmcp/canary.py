"""A daily live check of every platform: fetch a board we know should have
postings and confirm it still turns into real ones (a title and a link each).

If a vendor changes its markup or API, the parser usually does not crash, it
returns nothing or garbage. Sync would then close the board's postings; this
makes the break visible first (`jobmcp_canary_ok` in Prometheus, the
Platform health table in the admin portal, a warning in the log).

Which boards: the smallest few enabled boards of that platform that last
synced fine (real, current, cheap to fetch), then a fixed sample as a
fallback. Add your own with JOBMCP_CANARY_BOARDS="vendor:board,...".
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select

from . import config, db
from .orm import Board, CanaryResult, utcnow
from .services import ServiceError
from .vendors import VENDORS

log = logging.getLogger("jobmcp.canary")

# Well-known public boards, used only when the database has no good board for
# the platform yet.
SAMPLES: dict[str, list[str]] = {
    "greenhouse": ["stripe", "airbnb"],
    "lever": ["spotify", "palantir"],
    "ashby": ["notion", "openai"],
    "smartrecruiters": ["BoschGroup"],
    "recruitee": ["bunq"],
    "personio": ["personio.de"],
    "breezy": ["breezy"],
    "workday": ["nvidia.wd5/NVIDIAExternalCareerSite"],
    "icims": ["careers-360care"],
    "paylocity": ["9597a587-1c23-4def-9ecc-4901e647fab7"],
    "gem": ["fetch", "function-health"],
}
FROM_DB = 3  # boards tried from the database before the fixed samples
MIN_GOOD = 0.9  # share of postings that must have a title and a link


def configured() -> dict[str, list[str]]:
    out = {v: list(b) for v, b in SAMPLES.items()}
    for item in (config.CANARY_BOARDS or "").split(","):
        vendor, _, board = item.strip().partition(":")
        if vendor in VENDORS and board:
            out.setdefault(vendor, []).insert(0, board.strip())
    return out


def candidates(vendor: str) -> list[str]:
    with db.session() as s:
        recent = list(
            s.scalars(
                select(Board.board)
                .where(
                    Board.vendor == vendor,
                    Board.status == "enabled",
                    Board.last_ok_at.is_not(None),
                    Board.consecutive_failures == 0,
                    Board.job_count > 0,
                )
                .order_by(Board.job_count.asc())
                .limit(FROM_DB)
            )
        )
    seen = dict.fromkeys(recent + configured().get(vendor, []))
    return list(seen)


def judge(jobs: list) -> tuple[bool, str | None]:
    """Does this fetch look like real postings?"""
    if not jobs:
        return False, "no postings came back"
    good = sum(1 for j in jobs if j.title.strip() and j.url.startswith("http"))
    if good / len(jobs) < MIN_GOOD:
        return (
            False,
            f"only {good} of {len(jobs)} postings have a title and link",
        )
    return True, None


def check(vendor: str, fetcher=None) -> dict | None:
    """Try this platform's candidates in order; the first good one passes.
    None = nothing to check it with."""
    from .services import sync

    fetcher = fetcher or sync.default_fetcher
    tried = candidates(vendor)
    if not tried:
        return None
    last = {
        "vendor": vendor,
        "board": tried[0],
        "ok": False,
        "jobs": 0,
        "error": "not tried",
    }
    for board in tried:
        started = time.monotonic()
        try:
            jobs = fetcher(vendor, board)
            ok, why = judge(jobs)
            last = {
                "vendor": vendor,
                "board": board,
                "ok": ok,
                "jobs": len(jobs),
                "error": why,
            }
        except Exception as e:
            last = {
                "vendor": vendor,
                "board": board,
                "ok": False,
                "jobs": 0,
                "error": f"{type(e).__name__}: {e}"[:300],
            }
        log.debug(
            "canary %s/%s: %s in %.1fs",
            vendor,
            board,
            last["ok"],
            time.monotonic() - started,
        )
        if last["ok"]:
            break
    return last


def run(vendors: list[str] | None = None, fetcher=None) -> list[dict]:
    results = []
    for vendor in vendors or list(VENDORS):
        r = check(vendor, fetcher)
        if r is None:
            log.debug("canary %s: no board to check with", vendor)
            continue
        with db.session() as s:
            s.merge(CanaryResult(checked_at=utcnow(), **r))
        (log.info if r["ok"] else log.warning)(
            "canary %s: %s (%s, %d postings)%s",
            vendor,
            "ok" if r["ok"] else "FAILING",
            r["board"],
            r["jobs"],
            f": {r['error']}" if r["error"] else "",
        )
        results.append(r)
    return results


_lock = threading.Lock()
_running: dict | None = None


def start_background(fetcher=None) -> dict:
    """Run every check on a thread; one at a time. -> {"state": ...}"""
    global _running
    with _lock:
        if _running and _running["state"] == "running":
            raise ServiceError("A platform check is already running.", 409)
        _running = state = {"state": "running", "started_at": utcnow()}

    def go() -> None:
        try:
            state["results"] = run(fetcher=fetcher)
            state["state"] = "done"
        except Exception as e:
            log.exception("canary run failed")
            state["state"], state["error"] = "failed", str(e)[:300]
        from . import health

        try:
            health.snapshot_vendors()
        except Exception:
            log.exception("snapshot after canary failed")

    threading.Thread(target=go, name="jobmcp-canary", daemon=True).start()
    return {"state": "running", "started_at": state["started_at"]}
