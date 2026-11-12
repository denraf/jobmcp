from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

import logging

from . import config
from .orm import Base

log = logging.getLogger("livejobs.db")
_engine: Engine | None = None
_factory: sessionmaker | None = None


def mysql_url(url: str | None = None) -> str:
    """The configured URL, checked: livejobs runs on MySQL 8 only (via
    PyMySQL), always utf8mb4."""
    url = url or config.DATABASE_URL
    u = make_url(url)
    if u.drivername != "mysql+pymysql":
        raise ValueError(
            "livejobs needs MySQL, but the database URL starts with "
            f"{u.drivername!r}. "
            "Use "
            "mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE?charset=utf8mb4"
        )
    charset = u.query.get("charset")
    if charset is None:
        u = u.update_query_dict({"charset": "utf8mb4"})
    elif (
        charset != "utf8mb4"
        # emoji and non-Latin job titles need 4-byte UTF-8; utf8 (3-byte) would
        # truncate them
    ):
        raise ValueError(
            f"the database URL sets charset={charset!r}; "
            "livejobs requires charset=utf8mb4"
        )
    return u.render_as_string(hide_password=False)


def init(url: str | None = None) -> Engine:
    """(Re)bind the global engine. Called by entrypoints and tests."""
    global _engine, _factory
    # pre_ping + recycle: MySQL closes idle connections (wait_timeout), and a
    # long-lived web or worker process
    # must not hand out a dead one.
    _engine = create_engine(
        mysql_url(url), pool_pre_ping=True, pool_recycle=1800
    )
    u = _engine.url
    log.debug(
        "database engine bound: %s:%s/%s", u.host, u.port or 3306, u.database
    )
    _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def engine() -> Engine:
    return _engine or init()


def create_schema() -> None:
    Base.metadata.create_all(engine())


@contextmanager
def session():
    """One transaction: commit on success, roll back on error."""
    engine()
    assert _factory is not None
    s: Session = _factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
