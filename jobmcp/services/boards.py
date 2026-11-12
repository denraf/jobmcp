"""Board inventory and the admin's control over which slugs are synced."""

from __future__ import annotations

import re

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..orm import BOARD_STATUSES, Board, Job, utcnow
from ..vendors import VENDORS, classify
from . import ServiceError

CHUNK = 500


def upsert_discovered(s: Session, vendor: str, counts: dict[str, int]) -> int:
    """Record boards seen in Common Crawl. New ones start 'enabled' and due
    now, so the next sync run picks them up;
    a slug that turns out not to exist is disabled by the sync's own 404
    handling. Returns how many were new."""
    now, new = utcnow(), 0
    items = list(counts.items())
    for i in range(0, len(items), CHUNK):
        chunk = dict(items[i : i + CHUNK])
        existing = {
            b.board: b
            for b in s.scalars(
                select(Board).where(
                    Board.vendor == vendor, Board.board.in_(chunk)
                )
            )
        }
        for slug, n in chunk.items():
            b = existing.get(slug)
            if b:
                b.hits += n
                b.last_seen = now
            else:
                s.add(
                    Board(
                        vendor=vendor,
                        board=slug,
                        hits=n,
                        first_seen=now,
                        last_seen=now,
                        status="enabled",
                        next_sync_at=now,
                    )
                )
                new += 1
    s.flush()
    return new


def enable_discovered(s: Session) -> int:
    """Boards recorded as 'discovered' by an older version (or left over) start
    syncing too. Disabled ones stay off."""
    return s.execute(
        update(Board)
        .where(Board.status == "discovered")
        .values(status="enabled", next_sync_at=utcnow())
    ).rowcount


def get(s: Session, board_id: int) -> Board:
    b = s.get(Board, board_id)
    if not b:
        raise ServiceError("board not found", 404)
    return b


def resolve(
    vendor: str | None, slug: str | None = None, url: str | None = None
) -> tuple[str, str]:
    """(vendor, canonical board key) from either a vendor+slug or a pasted
    careers URL."""
    if url and url.strip():
        hit = classify(url.strip())
        if not hit:
            raise ServiceError(
                "that URL isn't a careers page on a supported platform "
                f"({', '.join(sorted(VENDORS))}); "
                "add it by platform and slug instead"
            )
        if vendor and vendor != hit[0]:
            raise ServiceError(f"that URL is a {hit[0]} board, not {vendor}")
        return hit
    if not vendor or vendor not in VENDORS:
        raise ServiceError(f"choose a platform: {', '.join(sorted(VENDORS))}")
    try:
        return vendor, VENDORS[vendor].normalize_board(slug or "")
    except ValueError as e:
        raise ServiceError(str(e))


def probe(vendor: str, board: str, fetcher=None) -> dict:
    """Ask the platform whether this board exists, right now, without storing
    anything.
    -> {exists, error, company, job_count, sample, public_url, jobs}  (`jobs`
    is for the caller; not for API output)."""
    import requests

    from ..vendors.base import BoardGone
    from . import (
        sync,
        # imported here so tests can replace sync.default_fetcher; avoids a
        # circular import
    )

    v = VENDORS[vendor]
    out = {
        "vendor": vendor,
        "board": board,
        "exists": False,
        "error": None,
        "company": "",
        "job_count": 0,
        "sample": [],
        "public_url": v.public_url(board),
        "jobs": [],
    }
    try:
        jobs = (fetcher or sync.default_fetcher)(vendor, board)
    except BoardGone:
        out["error"] = "not found on " + (v.label or vendor)
        return out
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        out["error"] = (
            f"not found on {v.label or vendor}"
            if code in (404, 410)
            else f"{v.label or vendor} answered HTTP {code}"
        )
        return out
    except (
        Exception
    ) as e:  # a timeout or a malformed answer is a failed check, not a crash
        out["error"] = f"could not check: {type(e).__name__}: {e}"[:300]
        return out
    out.update(
        exists=True,
        job_count=len(jobs),
        jobs=jobs,
        company=(jobs[0].company if jobs else ""),
        sample=list(dict.fromkeys(j.title for j in jobs))[:5],
    )  # distinct: one role is often posted per location
    return out


def public(p: dict) -> dict:
    return {k: v for k, v in p.items() if k != "jobs"}


def add_board(
    s: Session,
    vendor: str,
    slug: str,
    enable: bool = True,
    sync_interval_min: int | None = None,
    added_by: int | None = None,
    url: str | None = None,
) -> Board:
    """An admin adds a board by hand (one Common Crawl never saw, or one they
    want synced now).
    The key is canonicalised, so adding 'Stripe' finds the 'stripe' that
    discovery already stored rather than
    creating a second copy. Adding a board that discovery found *adopts* it: it
    becomes manual and enabled."""
    vendor, slug = resolve(vendor, slug, url)
    b = s.scalar(
        select(Board).where(Board.vendor == vendor, Board.board == slug)
    )
    if b is None:
        b = Board(vendor=vendor, board=slug, hits=0)
        s.add(b)
        s.flush()
    b.source = "manual"
    b.added_by = added_by if added_by is not None else b.added_by
    update_board(
        s,
        b.id,
        status="enabled" if enable else None,
        sync_interval_min=sync_interval_min,
    )
    return b


def parse_entry(line: str) -> tuple[str, str]:
    """'greenhouse:stripe' | 'greenhouse stripe' |
    'https://boards.greenhouse.io/stripe' | 'boards.greenhouse.io/stripe'."""
    line = line.strip()
    if "://" in line:
        return resolve(None, None, line)
    parts = re.split(r"[:\s]+", line, maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in VENDORS:
        return resolve(parts[0].lower(), parts[1])
    if (
        len(parts) == 1 and "." in line and "/" in line
    ):  # a URL pasted without its https://
        return resolve(None, None, "https://" + line)
    raise ServiceError(
        "write it as platform:slug (e.g. greenhouse:stripe) or paste the "
        "careers page URL"
    )


def add_many(
    s: Session,
    entries: list[str],
    enable: bool = True,
    verify: bool = True,
    added_by: int | None = None,
    workers: int = 16,
    fetcher=None,
    sync: bool = True,
    background: bool = False,
    chunk: int | None = None,
    progress=None,
) -> list[dict]:
    """Bulk add. Each entry is 'vendor:slug', 'vendor slug', or a careers URL.
    Returns one result per entry, in input
    order (added / exists / invalid / not_found), so a pasted list with a few
    bad lines still adds the good ones.

    Works through the list in small chunks (default: one per worker). Each
    chunk is checked on the platforms, saved
    and synced before the next begins, and its fetched jobs are then dropped,
    so memory stays flat however long the
    list is (holding every board's jobs at once is what runs a small container
    out of memory).
    `progress(chunk_results)` is called after each chunk."""
    from concurrent.futures import ThreadPoolExecutor

    chunk = chunk or max(1, workers)
    parsed: list[tuple[str, tuple[str, str] | None, str | None]] = []
    for raw in entries:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parsed.append((line, parse_entry(line), None))
        except ServiceError as e:
            parsed.append((line, None, str(e)))

    results: list[dict | None] = [None] * len(parsed)
    work, seen = (
        [],
        set(),
    )  # (index, line, key): what needs the network and the database
    for i, (line, key, err) in enumerate(parsed):
        if key is None:
            results[i] = {"input": line, "status": "invalid", "error": err}
        elif key in seen:
            results[i] = {
                "input": line,
                "vendor": key[0],
                "board": key[1],
                "status": "duplicate_in_list",
            }
        else:
            seen.add(key)
            work.append((i, line, key))
    if progress and any(results):
        progress([r for r in results if r])

    from . import sync as sync_svc

    for start in range(0, len(work), chunk):
        part = work[start : start + chunk]
        checks: dict[tuple[str, str], dict] = {}
        if verify:
            keys = [k for _, _, k in part]
            with ThreadPoolExecutor(
                max(1, min(workers, len(keys))), thread_name_prefix="check"
            ) as ex:
                checks = dict(
                    zip(
                        keys,
                        ex.map(lambda k: probe(k[0], k[1], fetcher), keys),
                    )
                )
        to_sync: dict[
            int, list | None
            # board id -> the jobs the check already fetched (None: fetch them)
        ] = {}
        done = []
        for i, line, key in part:
            chk = checks.get(key)
            if verify and not chk["exists"]:
                results[i] = {
                    "input": line,
                    "vendor": key[0],
                    "board": key[1],
                    "status": "not_found",
                    "error": chk["error"],
                }
            else:
                existed = s.scalar(
                    select(Board).where(
                        Board.vendor == key[0], Board.board == key[1]
                    )
                )
                was_enabled = bool(
                    existed
                    and existed.status == "enabled"
                    and existed.source == "manual"
                )
                b = add_board(s, key[0], key[1], enable, None, added_by)
                results[i] = {
                    "input": line,
                    "vendor": key[0],
                    "board_id": b.id,
                    "board": key[1],
                    "status": "exists" if was_enabled else "added",
                    "job_count": chk["job_count"] if chk else None,
                }
                if enable and not was_enabled:
                    to_sync[b.id] = chk.pop("jobs") if chk else None
            done.append(results[i])
        # the sync threads use their own sessions and must see the boards
        s.commit()
        if sync and to_sync:
            sync_svc.sync_many(to_sync, workers, fetcher, background)
        del (
            checks,
            to_sync,
        )  # free this chunk's jobs before the next chunk is fetched
        if progress:
            progress(done)
    return [r for r in results if r]


def update_board(
    s: Session,
    board_id: int,
    status: str | None = None,
    sync_interval_min: int | None = None,
    clear_interval: bool = False,
    is_agency: bool | None = None,
    clear_agency: bool = False,
) -> Board:
    b = get(s, board_id)
    if is_agency is not None:
        b.agency_override = b.is_agency = is_agency
    if clear_agency:
        from .. import classify

        b.agency_override = None
        b.is_agency = classify.is_agency(b.company_name, b.board)
    if status is not None:
        if status not in BOARD_STATUSES:
            raise ServiceError(f"status must be one of {BOARD_STATUSES}")
        if status == "enabled" and b.status != "enabled":
            b.next_sync_at = utcnow()  # sync on the next worker tick
            b.consecutive_failures = b.consecutive_gone = 0
        b.status = status
    if sync_interval_min is not None:
        if sync_interval_min < 5:
            raise ServiceError("sync_interval_min must be >= 5")
        b.sync_interval_min = sync_interval_min
    if clear_interval:
        b.sync_interval_min = None
    return b


def bulk_set_status(
    s: Session,
    vendor: str | None,
    from_status: str,
    to_status: str,
    min_hits: int = 0,
    limit: int | None = None,
) -> int:
    """e.g. enable the 500 most-referenced discovered Greenhouse boards."""
    if to_status not in BOARD_STATUSES or from_status not in BOARD_STATUSES:
        raise ServiceError(f"status must be one of {BOARD_STATUSES}")
    q = select(Board.id).where(
        Board.status == from_status, Board.hits >= min_hits
    )
    if vendor:
        q = q.where(Board.vendor == vendor)
    q = q.order_by(Board.hits.desc(), Board.id)
    if limit:
        q = q.limit(limit)
    ids = list(s.scalars(q))
    for i in range(0, len(ids), CHUNK):
        part = ids[i : i + CHUNK]
        vals = {"status": to_status}
        if to_status == "enabled":
            vals.update(
                next_sync_at=utcnow(),
                consecutive_failures=0,
                consecutive_gone=0,
            )
        s.execute(update(Board).where(Board.id.in_(part)).values(**vals))
    return len(ids)


SORTS = {
    "board": Board.board,
    "status": Board.status,
    "jobs": Board.job_count,
    "synced": Board.last_sync_at,
}


def list_boards(
    s: Session,
    status: str | None = None,
    vendor: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    source: str | None = None,
    sort: str | None = None,
    direction: str = "asc",
) -> list[Board]:
    stmt = select(Board)
    if source:
        stmt = stmt.where(Board.source == source)
    if status:
        stmt = stmt.where(Board.status == status)
    if vendor:
        stmt = stmt.where(Board.vendor == vendor)
    if q:
        stmt = stmt.where(Board.board.contains(q, autoescape=True))
    if sort:
        if sort not in SORTS or direction not in ("asc", "desc"):
            raise ServiceError(
                f"sort must be one of {sorted(SORTS)}, direction asc or desc"
            )
        col = SORTS[sort]
        order = [col.desc() if direction == "desc" else col.asc(), Board.id]
        if sort == "synced":
            order.insert(
                0, col.is_(None)
            )  # boards never synced go last in either direction
    else:
        order = [
            Board.hits.desc(),
            Board.id,
        ]  # the default: most referenced by Common Crawl first
    return list(
        s.scalars(stmt.order_by(*order).limit(min(limit, 500)).offset(offset))
    )


def counts(s: Session) -> dict:
    """How many boards there are, and how they split by status and by how they
    were added."""
    out = {
        "total": 0,
        "enabled": 0,
        "discovered": 0,
        "disabled": 0,
        "manual": 0,
        "crawl": 0,
    }
    for status, source, n in s.execute(
        select(Board.status, Board.source, func.count()).group_by(
            Board.status, Board.source
        )
    ).all():
        out["total"] += n
        out[status] = out.get(status, 0) + n
        out[source] = out.get(source, 0) + n
    return out


def stats(s: Session) -> list[dict]:
    rows = s.execute(
        select(
            Board.vendor,
            Board.status,
            func.count(),
            func.coalesce(func.sum(Board.job_count), 0),
        ).group_by(Board.vendor, Board.status)
    ).all()
    out: dict[str, dict] = {}
    for vendor, status, n, jobs in rows:
        d = out.setdefault(
            vendor,
            {
                "vendor": vendor,
                "discovered": 0,
                "enabled": 0,
                "disabled": 0,
                "jobs": 0,
            },
        )
        d[status] = n
        d["jobs"] += int(jobs)
    active = dict(
        s.execute(
            select(Job.vendor, func.count())
            .where(Job.active == 1)
            .group_by(Job.vendor)
        ).all()
    )
    for v, d in out.items():
        d["active_jobs"] = active.get(v, 0)
    return sorted(
        out.values(),
        key=lambda d: -(d["discovered"] + d["enabled"] + d["disabled"]),
    )
