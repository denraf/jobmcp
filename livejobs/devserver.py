"""`livejobs dev`: everything needed to try the product on one machine, in one
process.

Needs a MySQL database (scripts/dev.sh starts one with Docker Compose, or point
LIVEJOBS_DATABASE_URL at your own).
It then sets up the tables, a verified admin and user, sample boards, the sync
worker on a background thread and the
website. Emails are printed to this terminal instead of sent.
"""

from __future__ import annotations

import logging
import os
import threading

import uvicorn
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from . import config, db, migrations
from .orm import Base, Board, User
from .services import auth, boards, prefs, sync

log = logging.getLogger("livejobs.dev")

# What `docker compose up -d db` provides (see docker-compose.yml).
DEFAULT_URL = "mysql+pymysql://livejobs:livejobs@127.0.0.1:3306/livejobs_dev?charset=utf8mb4"
ADMIN = ("admin@example.com", "dev-admin-password")
USER = ("user@example.com", "dev-user-password")
SAMPLE_BOARDS = [
    ("ashby", "1password"),
    ("lever", "spotify"),
    ("greenhouse", "stripe"),
]


def _ensure_user(email: str, password: str, role: str) -> None:
    with db.session() as s:
        if not s.scalar(select(User).where(User.email == email)):
            u = auth.create_user(s, email, password, role)
            if role == "user":
                prefs.update_preferences(
                    s,
                    u,
                    {
                        "title_include": ["engineer*"],
                        "title_exclude": ["intern"],
                    },
                )


def _check_database(url: str) -> None:
    try:
        with db.engine().connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:
        u = make_url(url)
        raise SystemExit(
            f"\nCannot reach MySQL at {u.host}:{u.port or 3306} as "
            f"{u.username!r} (database {u.database!r}):\n  "
            f"{e.__class__.__name__}: "
            f"{str(e).splitlines()[0][:200]}\n\n"
            "livejobs runs on MySQL. Either:\n"
            "  - let scripts/dev.sh start one with Docker:  docker compose up "
            "-d db   (then re-run), or\n"
            "  - point LIVEJOBS_DATABASE_URL at a MySQL 8 database you can "
            "write to, e.g.\n"
            "      "
            "mysql+pymysql://user:password@127.0.0.1:3306/livejobs_dev?charset=utf8mb4\n"
        )


def run(
    db_url: str | None, port: int, seed: bool = True, reset: bool = False
) -> None:
    url = db_url or os.environ.get("LIVEJOBS_DATABASE_URL") or DEFAULT_URL
    db.init(url)
    _check_database(url)
    name = make_url(url).database or ""
    if reset:
        if not name.endswith(
            "_dev"
        ):  # never wipe a database that might hold real data
            raise SystemExit(
                "--reset drops every table, so it only runs on a database "
                "whose name ends in '_dev' "
                f"(this one is {name!r})."
            )
        Base.metadata.drop_all(db.engine())
        db.engine().dispose()
        with (
            db.engine().begin() as c
        ):  # tables from older versions of the schema are not in the metadata
            c.execute(text("DROP TABLE IF EXISTS profiles"))
    migrations.ensure_schema()

    # Local settings. Mail is printed rather than sent, so verification and
    # reset links appear in this terminal.
    config.DEV_MODE = True
    config.PUBLIC_BASE_URL = f"http://localhost:{port}"
    config.PUBLIC_MCP_URL = f"http://localhost:{port}/mcp/"
    config.MAIL_BACKEND = os.environ.get("LIVEJOBS_MAIL_BACKEND", "console")

    _ensure_user(*ADMIN, "admin")
    _ensure_user(*USER, "user")
    if seed:
        with db.session() as s:
            for vendor, slug in SAMPLE_BOARDS:
                if not s.scalar(
                    select(Board).where(
                        Board.vendor == vendor, Board.board == slug
                    )
                ):
                    boards.add_board(s, vendor, slug, enable=True)

    stop = threading.Event()
    worker = threading.Thread(
        target=sync.run_forever,
        args=(stop,),
        kwargs={"poll_seconds": 30},
        daemon=True,
    )
    worker.start()

    u = make_url(url)
    print(
        f"""
  livejobs is starting in development mode

    Site              http://localhost:{port}
    API docs          http://localhost:{port}/api/docs
    Metrics           http://localhost:{port}/metrics
    MCP endpoint      http://localhost:{port}/mcp/
    Database          MySQL {u.host}:{u.port or 3306}/{u.database}

    Admin             {ADMIN[0]} / {ADMIN[1]}
    Test user         {USER[0]} / {USER[1]}     (prefers "engineer*" titles)

    Sign up with any address: the verification link is printed below instead
    of emailed.
    Sample boards sync in the background ({"on" if seed else "off"}); the
    first jobs appear within a minute.
    Ctrl-C to stop.
""",
        flush=True,
    )
    try:
        uvicorn.run(
            "livejobs.site:app", host="127.0.0.1", port=port, log_config=None
        )
    finally:
        stop.set()
