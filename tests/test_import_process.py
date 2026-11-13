"""A long board import runs in its own process, so it cannot take the web
service's interpreter lock from the pages."""

import time

from sqlalchemy import func, select

from jobmcp import config, db
from jobmcp.orm import Board
from jobmcp.services import imports

from .conftest import TEST_URL


def wait(job_id):
    for _ in range(300):
        j = imports.get(job_id)
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("import did not finish")


def test_a_long_import_runs_in_a_child_process(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "IMPORT_IN_PROCESS", False)
    monkeypatch.setenv("JOBMCP_DATABASE_URL", TEST_URL)
    lines = [f"lever:child{i}" for i in range(25)] + ["nonsense line"]
    # enable=False and verify=False: nothing is fetched, so no network
    job = imports.start(lines, enable=False, verify=False, admin_id=1)
    j = wait(job["id"])
    assert j["state"] == "done", j
    assert j["done"] == 26 and j["summary"] == {"added": 25, "invalid": 1}
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Board)) == 25


def test_a_failing_child_is_reported_not_hung(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "IMPORT_IN_PROCESS", False)
    # a database the child cannot use
    monkeypatch.setenv(
        "JOBMCP_DATABASE_URL",
        "mysql+pymysql://nobody:x@127.0.0.1:1/none_test?charset=utf8mb4",
    )
    job = imports.start(
        [f"lever:x{i}" for i in range(25)],
        enable=False,
        verify=False,
        admin_id=1,
    )
    j = wait(job["id"])
    assert j["state"] == "failed" and j["error"]
