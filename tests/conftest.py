import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from jobmcp import config, db, mailer, metrics, ratelimit
from jobmcp.orm import Base

# The same database docker-compose.yml creates. Override with
# JOBMCP_TEST_DATABASE_URL (see scripts/test.sh).
DEFAULT_TEST_URL = (
    "mysql+pymysql://jobmcp:jobmcp@127.0.0.1:3306/jobmcp_test?charset=utf8mb4"
)
TEST_URL = os.environ.get("JOBMCP_TEST_DATABASE_URL") or DEFAULT_TEST_URL


@pytest.fixture(autouse=True)
def _no_background_sync(monkeypatch):
    """Tests never start a real sync of boards that were just discovered (it
    would go to the network)."""
    monkeypatch.setattr(config, "SYNC_AFTER_REFRESH", False)


@pytest.fixture(autouse=True)
def _no_response_cache(monkeypatch):
    """Tests sync and then query again within seconds, so the public list's
    cache is off (tests/test_cache.py turns it on)."""
    from jobmcp import cache

    monkeypatch.setattr(config, "CACHE_SECONDS", 0)
    cache.public.clear()


@pytest.fixture(scope="session", autouse=True)
def _mysql():
    """The suite runs on MySQL only. Fail loudly and helpfully, and never touch
    a database that isn't a test one."""
    try:
        db.mysql_url(
            TEST_URL
        )  # a non-MySQL URL is refused here, before anything else is looked at
    except ValueError as e:
        pytest.exit(
            f"JOBMCP_TEST_DATABASE_URL is not usable: {e}", returncode=2
        )
    name = make_url(TEST_URL).database or ""
    if not name.endswith("_test"):
        pytest.exit(
            "Refusing to run: the tests drop and recreate every table, "
            f"and the database {name!r} "
            "does not end in '_test'. Set JOBMCP_TEST_DATABASE_URL to a "
            "dedicated test database.",
            returncode=2,
        )
    try:
        db.init(TEST_URL)
        with db.engine().connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:
        pytest.exit(
            "MySQL is not reachable for the tests:\n"
            f"  {TEST_URL.split('@')[-1]}\n  "
            f"{type(e).__name__}: {str(e).splitlines()[0][:160]}\n"
            "Run scripts/test.sh (starts MySQL with Docker), or set "
            "JOBMCP_TEST_DATABASE_URL to a MySQL 8 database "
            "whose name ends in _test.",
            returncode=2,
        )


@pytest.fixture()
def fresh_db():
    """An empty schema on the MySQL test database, with the app's mail and
    metrics state reset."""
    ratelimit.reset()
    mailer.outbox.clear()
    metrics.collector.reset()
    config.MAIL_BACKEND = "memory"
    config.PUBLIC_BASE_URL = "https://jobs.test"
    config.REQUIRE_EMAIL_VERIFICATION = True
    config.EMAIL_COOLDOWN_SECONDS = 60
    db.init(TEST_URL)
    Base.metadata.drop_all(db.engine())
    with (
        db.engine().begin() as c
        # leftovers from an older schema (a removed table) would otherwise
        # linger
    ):
        c.execute(text("DROP TABLE IF EXISTS profiles"))
    Base.metadata.create_all(db.engine())
    yield
    Base.metadata.drop_all(db.engine())
