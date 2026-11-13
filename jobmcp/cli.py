from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import threading

from . import config, db, logs
from .vendors import VENDORS, classify


def _password() -> str:
    return os.environ.get("JOBMCP_ADMIN_PASSWORD") or getpass.getpass(
        "Password: "
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="jobmcp")
    ap.add_argument(
        "--db-url",
        help="MySQL URL, mysql+pymysql://user:pw@host/db?charset=utf8mb4 "
        "(default: $JOBMCP_DATABASE_URL)",
    )
    ap.add_argument(
        "--log-level",
        choices=[*logs.LEVELS, *(x.lower() for x in logs.LEVELS)],
        metavar="LEVEL",
        help="DEBUG, INFO, WARNING or ERROR (default: $JOBMCP_LOG_LEVEL, "
        "else INFO)",
    )
    ap.add_argument(
        "--log-format",
        choices=["text", "json"],
        help="text or json (default: $JOBMCP_LOG_FORMAT, else text)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "init-db",
        help="create tables and apply schema upgrades (safe to run on every "
        "deploy)",
    )

    p = sub.add_parser(
        "create-admin",
        help="bootstrap an admin (password from $JOBMCP_ADMIN_PASSWORD or "
        "prompt)",
    )
    p.add_argument("--email", required=True)

    p = sub.add_parser(
        "inventory",
        help="discover boards from Common Crawl (they land as 'discovered')",
    )
    p.add_argument("--crawls", type=int, default=1)
    p.add_argument("--crawl", action="append")
    p.add_argument("--vendor", action="append", choices=sorted(VENDORS))
    p.add_argument(
        "--workers",
        type=int,
        default=config.CC_WORKERS,
        help="Common Crawl threads (separate from the sync pool)",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="the scheduled monthly mode: ingest the newest crawl unless it "
        "is already done "
        "(exit 0 = done/up to date, 75 = incomplete, run again later)",
    )

    p = sub.add_parser("boards", help="list / enable / disable synced slugs")
    bs = p.add_subparsers(dest="bcmd", required=True)
    q = bs.add_parser("list")
    q.add_argument("--status", choices=["discovered", "enabled", "disabled"])
    q.add_argument("--vendor", choices=sorted(VENDORS))
    q.add_argument("--limit", type=int, default=50)
    q = bs.add_parser(
        "add", help="add and enable a slug (even one Common Crawl never saw)"
    )
    q.add_argument("vendor", choices=sorted(VENDORS))
    q.add_argument("slug")
    q.add_argument("--interval", type=int, help="sync every N minutes")
    q.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the check that the board exists on its platform",
    )
    q = bs.add_parser("add-url", help="add boards from careers page URLs")
    q.add_argument("urls", nargs="+")
    q.add_argument("--no-verify", action="store_true")
    q = bs.add_parser(
        "import",
        help="add many boards from a file (one 'platform:slug' or URL per "
        "line, - for stdin)",
    )
    q.add_argument("file")
    q.add_argument("--no-verify", action="store_true")
    for name in ("enable", "disable"):
        q = bs.add_parser(name)
        q.add_argument("vendor", choices=sorted(VENDORS))
        q.add_argument("slug")
    q = bs.add_parser(
        "enable-top", help="enable the N most-referenced discovered boards"
    )
    q.add_argument("n", type=int)
    q.add_argument("--vendor", choices=sorted(VENDORS))
    bs.add_parser("stats")

    p = sub.add_parser(
        "worker", help="sync enabled boards on their schedule, forever"
    )
    p.add_argument("--workers", type=int, default=config.SYNC_WORKERS)
    p.add_argument(
        "--poll",
        type=int,
        default=30,
        help="seconds between checks for due boards",
    )

    sub.add_parser(
        "import-run",
        help="(internal) run one bulk board import from stdin; the web "
        "service starts this so the import does not slow the pages down",
    )
    p = sub.add_parser(
        "sync-once",
        help="sync everything due (for cron / a systemd timer), then exit",
    )
    p.add_argument("--workers", type=int, default=config.SYNC_WORKERS)
    p.add_argument(
        "--slack",
        type=int,
        default=0,
        metavar="MIN",
        help="also sync boards due within MIN minutes; use 10 with an hourly "
        "cron",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="force",
        help="re-fetch every enabled board now, ignoring the schedule (e.g. "
        "to refresh stored descriptions)",
    )

    p = sub.add_parser(
        "canary",
        help="check every platform against a live board now; exit 1 if any "
        "fails",
    )
    p.add_argument("--vendor", action="append", choices=sorted(VENDORS))

    p = sub.add_parser(
        "serve", help="website + REST API (/api) + MCP (/mcp) in one process"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser(
        "mcp", help="MCP server (stdio by default; --http for remote clients)"
    )
    p.add_argument("--http", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser(
        "wait-db",
        help="wait until MySQL accepts connections (and with --schema, until "
        "the tables are ready)",
    )
    p.add_argument(
        "--schema",
        action="store_true",
        help="also wait for the schema (created by init-db) to be complete",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="seconds to wait before giving up (default 180)",
    )

    p = sub.add_parser(
        "ensure-admin",
        help="create the first admin if it doesn't exist yet; never changes "
        "an existing account",
    )
    p.add_argument(
        "--email",
        default=os.environ.get("JOBMCP_ADMIN_EMAIL"),
        help="default: $JOBMCP_ADMIN_EMAIL",
    )

    p = sub.add_parser(
        "scheduler",
        help="run the recurring jobs: hourly job fetch and the monthly "
        "Common Crawl refresh",
    )
    p.add_argument(
        "--no-initial-sync",
        action="store_true",
        help="don't fetch immediately at startup",
    )

    p = sub.add_parser(
        "send-test-email",
        help="send a test message with the configured mail settings (check "
        "your SMTP setup)",
    )
    p.add_argument("--to", required=True)

    p = sub.add_parser(
        "dev",
        help="run everything locally: database, sample users, sync worker "
        "and the site",
    )
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--no-seed",
        action="store_true",
        help="don't add and sync the sample boards",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="drop and recreate every table first (only for a database whose "
        "name ends in _dev)",
    )

    sub.add_parser("classify", help="vendor/board for a URL").add_argument(
        "url"
    )
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    logs.setup(a.log_level, a.log_format)
    logging.getLogger("jobmcp.cli").debug("command: %s", a.cmd)
    if a.cmd == "classify":
        return print(classify(a.url) or "no match")
    if a.cmd == "import-run":
        from .services import imports

        return imports.child_main()
    if a.cmd == "wait-db":
        return _wait_db(a)
    if a.cmd == "ensure-admin":
        return _ensure_admin(a)
    if a.cmd == "scheduler":
        from . import scheduler

        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        scheduler.Scheduler(
            *scheduler.default_jobs(), canary_fn=scheduler.canary_job()
        ).run(stop, initial_sync=not a.no_initial_sync)
        return
    if a.cmd == "send-test-email":
        from . import mailer

        print(
            f"backend={config.MAIL_BACKEND} "
            f"host={config.SMTP_HOST}:{config.SMTP_PORT} "
            f"security={config.SMTP_SECURITY} from={config.MAIL_FROM}"
        )
        if config.MAIL_BACKEND != "smtp":
            print(
                "Note: JOBMCP_MAIL_BACKEND is not 'smtp', so this will not "
                "leave the machine."
            )
        ok = mailer.send(
            mailer.Mail(
                a.to,
                "jobmcp test email",
                "If you can read this, jobmcp can send email.\n",
                "test",
            ),
            wait=True,
        )
        if not ok:
            raise SystemExit(
                "failed: the mail server did not accept the message (the "
                "error is logged above)"
            )
        return print(
            "accepted by the mail server; check the inbox (and spam)"
            if config.MAIL_BACKEND == "smtp"
            else "logged above"
        )
    if a.cmd == "dev":
        from . import devserver

        return devserver.run(
            a.db_url, a.port, seed=not a.no_seed, reset=a.reset
        )
    try:
        db.init(a.db_url)
    # a bad database URL (e.g. not MySQL): print the reason, not a traceback
    except ValueError as e:
        raise SystemExit(f"error: {e}")

    if a.cmd == "init-db":
        from . import migrations

        changed = migrations.ensure_schema()
        print(
            "schema ready"
            + (f" (applied: {', '.join(changed)})" if changed else "")
        )
    elif a.cmd == "create-admin":
        from .services import auth

        with db.session() as s:
            u = auth.create_user(s, a.email, _password(), role="admin")
            print(f"admin {u.email} created (id {u.id})")
    elif a.cmd == "inventory":
        from . import inventory

        if a.refresh:
            status, _ = inventory.refresh(a.vendor, a.workers, trigger="cli")
            raise SystemExit(
                {
                    "complete": 0,
                    "up_to_date": 0,
                    "already_running": 0,
                    "partial": 75,
                    "failed": 1,
                }[status]
            )
        inventory.build(
            a.crawl or inventory.list_crawls(a.crawls), a.vendor, a.workers
        )
    elif a.cmd == "boards":
        _boards(a)
    elif a.cmd == "worker":
        from .services import sync

        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        sync.run_forever(stop, a.workers, poll_seconds=a.poll)
    elif a.cmd == "sync-once":
        from .services import sync

        res = sync.run_due(
            a.workers, slack_min=a.slack, until_empty=True, force=a.force
        )
        print(
            f"synced {len(res)} boards: {sum(r['ok'] for r in res)} ok, "
            f"{sum(not r['ok'] for r in res)} failed, "
            f"{sum(r['jobs'] for r in res)} jobs, "
            f"{sum(r['new'] for r in res)} new, "
            f"{sum(r['closed'] for r in res)} closed"
        )
    elif a.cmd == "canary":
        from . import canary, health

        results = canary.run(a.vendor)
        health.snapshot_vendors()
        for r in results:
            print(
                f"{'ok' if r['ok'] else 'FAIL':<5}{r['vendor']:<16}"
                f"{r['board']:<40}{r['jobs']:>6} postings"
                + (f"  {r['error']}" if r["error"] else "")
            )
        if not results:
            print("no platform has a board to check yet")
        if any(not r["ok"] for r in results):
            raise SystemExit(1)
    elif a.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "jobmcp.site:app",
            host=a.host,
            port=a.port,
            proxy_headers=config.TRUST_PROXY,
            log_config=None,
        )
    elif a.cmd == "mcp":
        from . import mcp_server

        if a.http:
            import uvicorn

            uvicorn.run(
                mcp_server.http_app(),
                host=a.host,
                port=a.port,
                log_config=None,
            )
        else:
            mcp_server.run_stdio()


def _wait_db(a) -> None:
    """Block until MySQL answers (and the schema is ready). Containers start in
    any order; this is how they cope.
    It reports progress, because a silent wait looks exactly like a hung
    container."""
    import socket
    import time

    from sqlalchemy import text
    from . import migrations

    # the engine's URL, which may come from --db-url rather than the
    # environment
    host = db.engine().url.host
    start = time.monotonic()
    deadline, last, next_note = start + a.timeout, None, start + 10
    while time.monotonic() < deadline:
        try:
            with db.engine().connect() as c:
                c.execute(text("SELECT 1"))
            if not a.schema or migrations.is_current():
                return print(
                    "database ready"
                    + (" (schema current)" if a.schema else ""),
                    flush=True,
                )
            last = (
                "connected; waiting for the web service to create the schema"
            )
        except Exception as e:
            last = f"{type(e).__name__}: {str(e).splitlines()[0][:140]}"
            try:
                socket.getaddrinfo(host, None)
            except OSError:
                last = f"the database host {host!r} does not resolve ({last})"
        if time.monotonic() >= next_note:
            print(
                "still waiting for the database "
                f"({int(time.monotonic() - start)}s): {last}",
                flush=True,
            )
            next_note += 10
        time.sleep(2)
    hint = ""
    if last and "does not resolve" in last:
        hint = (
            f"\nThe host name {host!r} is not known to this container. With "
            "Docker Compose service names resolve on their own. With "
            "Apple's container, register the DNS domain once (needs "
            "admin rights):\n"
            "    sudo container system dns create jobmcp\nthen restart the "
            "stack."
        )
    raise SystemExit(
        f"gave up after {a.timeout}s waiting for the database: {last}{hint}"
    )


def _ensure_admin(a) -> None:
    import secrets

    from sqlalchemy import select

    from .orm import User
    from .services import auth

    if not a.email:
        return print(
            "no admin email given (JOBMCP_ADMIN_EMAIL); not creating an admin"
        )
    email = a.email.strip().lower()
    with db.session() as s:
        if s.scalar(select(User).where(User.email == email)):
            return print(f"admin {email} already exists; leaving it untouched")
        password = os.environ.get(
            "JOBMCP_ADMIN_PASSWORD"
        ) or secrets.token_urlsafe(16)
        generated = not os.environ.get("JOBMCP_ADMIN_PASSWORD")
        auth.create_user(s, email, password, role="admin")
    print(f"created admin {email}")
    if generated:  # shown once, here, and stored only as a hash
        print(
            "  generated password (shown once, change it in your portal): "
            f"{password}"
        )


def _boards(a):
    from sqlalchemy import select

    from .orm import Board
    from .services import boards

    with db.session() as s:
        if a.bcmd == "list":
            for b in boards.list_boards(s, a.status, a.vendor, limit=a.limit):
                print(
                    f"{b.id:>7} {b.vendor:<16}{b.board:<40}{b.status:<11}"
                    f"hits={b.hits:<5}jobs={b.job_count}"
                )
        elif a.bcmd in ("add", "add-url", "import"):
            if a.bcmd == "add":
                entries = [f"{a.vendor}:{a.slug}"]
            elif a.bcmd == "add-url":
                entries = a.urls
            else:
                import sys

                entries = (
                    (sys.stdin if a.file == "-" else open(a.file))
                    .read()
                    .splitlines()
                )
            results = boards.add_many(s, entries, True, not a.no_verify)
            if a.bcmd == "add" and a.interval:
                for r in results:
                    if r.get("board_id"):
                        boards.update_board(
                            s, r["board_id"], sync_interval_min=a.interval
                        )
            for r in results:
                tail = r.get("error") or (
                    f"{r['job_count']} jobs"
                    if r.get("job_count") is not None
                    else ""
                )
                print(
                    f"{r['status']:<18}{r.get('vendor', ''):<16}"
                    f"{r.get('board', r['input']):<40}{tail}"
                )
            if any(r["status"] in ("invalid", "not_found") for r in results):
                s.commit()
                raise SystemExit(1)
        elif a.bcmd in ("enable", "disable"):
            b = s.scalar(
                select(Board).where(
                    Board.vendor == a.vendor, Board.board == a.slug
                )
            )
            if not b:
                raise SystemExit(
                    "unknown board (use `boards add` to add a slug Common "
                    "Crawl hasn't seen)"
                )
            boards.update_board(s, b.id, status=a.bcmd + "d")
            print(f"{a.bcmd}d {a.vendor}:{a.slug}")
        elif a.bcmd == "enable-top":
            print(
                "enabled",
                boards.bulk_set_status(
                    s, a.vendor, "discovered", "enabled", limit=a.n
                ),
            )
        elif a.bcmd == "stats":
            print(
                f"{'vendor':<18}{'discovered':>11}{'enabled':>9}"
                f"{'disabled':>9}{'active jobs':>12}"
            )
            for d in boards.stats(s):
                print(
                    f"{d['vendor']:<18}{d['discovered']:>11}{d['enabled']:>9}{d['disabled']:>9}{d['active_jobs']:>12}"
                )
