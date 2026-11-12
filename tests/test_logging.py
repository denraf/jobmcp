import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from livejobs import logs

from .helpers import mk_user


@pytest.fixture()
def captured(monkeypatch):
    """Run logs.setup() against a buffer instead of stdout, then restore the
    root logger."""
    root = logging.getLogger()
    saved_h, saved_l = list(root.handlers), root.level
    buf = io.StringIO()
    real = logs.setup
    # pytest swaps sys.stdout around each phase, so hand the handler the buffer
    # directly
    monkeypatch.setattr(
        logs, "setup", lambda *a, **k: real(*a, **{**k, "stream": buf})
    )
    yield buf
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_h:
        root.addHandler(h)
    root.setLevel(saved_l)
    for n in ("livejobs.sync", "livejobs.http", "uvicorn.access"):
        logging.getLogger(n).setLevel(logging.NOTSET)


def test_level_comes_from_the_environment(captured, monkeypatch):
    monkeypatch.setattr("livejobs.config.LOG_LEVEL", "warning")
    logs.setup()
    logging.getLogger("livejobs.x").info("hidden")
    logging.getLogger("livejobs.x").warning("shown")
    assert (
        "shown" in captured.getvalue() and "hidden" not in captured.getvalue()
    )


def test_debug_and_per_logger_overrides(captured):
    logs.setup("INFO", "text", "livejobs.sync=DEBUG")
    logging.getLogger("livejobs.sync").debug("sync detail")
    logging.getLogger("livejobs.other").debug("other detail")
    out = captured.getvalue()
    assert "sync detail" in out and "other detail" not in out


def test_json_format_is_one_object_per_line_with_extras(captured):
    logs.setup("INFO", "json")
    logging.getLogger("livejobs.api").info("login", extra={"user_id": 7})
    rec = json.loads(captured.getvalue().strip().splitlines()[-1])
    assert (
        rec["msg"] == "login"
        and rec["user_id"] == 7
        and rec["level"] == "INFO"
        and rec["logger"] == "livejobs.api"
    )


def test_bad_level_is_refused_with_a_message(captured, capsys):
    with pytest.raises(SystemExit) as e:
        logs.setup("loud")
    assert e.value.code == 2 and "log level" in capsys.readouterr().err
    with pytest.raises(ValueError):
        logs.parse_overrides("nonsense")


def test_noisy_libraries_are_quiet_even_at_debug(captured):
    logs.setup("DEBUG", "text")
    assert logging.getLogger("urllib3").level == logging.WARNING


@pytest.mark.usefixtures("fresh_db")
def test_requests_are_logged_without_query_strings_or_credentials(captured):
    from livejobs.site import app

    logs.setup("INFO", "text")
    mk_user("a@x.io", "user")
    c = TestClient(app)
    c.post(
        "/api/auth/login",
        json={"email": "a@x.io", "password": "wrong-password-here"},
    )
    r = c.get("/api/public/config?token=secretquery")
    out = captured.getvalue()
    assert "POST /api/auth/login 401" in out and "login failed" in out
    assert "GET /api/public/config 200" in out and "secretquery" not in out
    assert "wrong-password-here" not in out and "a@x.io" not in out
    assert (
        r.headers["x-request-id"] and f"req={r.headers['x-request-id']}" in out
    )
    assert "/healthz" not in (
        c.get("/healthz") and captured.getvalue()
    )  # quiet at INFO


@pytest.mark.usefixtures("fresh_db")
def test_sync_logs_per_board_at_debug_and_a_summary_at_info(captured):
    from livejobs.services import sync
    from .helpers import fj, mk_board

    ids = [mk_board("lever", "a"), mk_board("lever", "b")]
    logs.setup("INFO", "text")
    sync.sync_many(ids, 2, lambda v, b: [fj("T", board=b, vendor=v)])
    out = captured.getvalue()
    assert "synced 2 boards" in out and "sync ok lever/a" not in out
    logs.setup("INFO", "text", "livejobs.sync=DEBUG")
    sync.sync_many(ids, 2, lambda v, b: [fj("T", board=b, vendor=v)])
    assert "sync ok lever/a" in captured.getvalue()


@pytest.mark.usefixtures("fresh_db")
def test_failed_and_disabled_boards_are_logged(captured):
    import requests
    from livejobs.services import sync
    from .helpers import mk_board

    bid = mk_board("lever", "gone")
    logs.setup("INFO", "text")

    def boom(v, b):
        r = requests.Response()
        r.status_code = 404
        raise requests.HTTPError(response=r)

    sync.sync_many([bid], 1, boom)
    assert "sync failed lever/gone: HTTP 404" in captured.getvalue()


@pytest.mark.usefixtures("fresh_db")
def test_a_refused_token_says_why(captured):
    from livejobs import db
    from livejobs.services import auth
    from livejobs.orm import User
    from .helpers import mk_user

    uid = mk_user("a@x.io")
    logs.setup("INFO", "text")
    with db.session() as s:
        u = s.get(User, uid)
        raw, t = auth.issue_token(s, u, "mcp", name="x")
        sess, _ = auth.issue_token(s, u, "session")
        assert auth.resolve_token(s, "lj_m_nonsense", "mcp") is None
        assert auth.resolve_token(s, sess, "mcp") is None
        auth.revoke_token(s, u, t.id)
        assert auth.resolve_token(s, raw, "mcp") is None
    out = captured.getvalue()
    assert (
        "no such token" in out
        and "is a session token, not a mcp token" in out
        and "was revoked" in out
    )
    assert raw not in out and sess not in out


def test_http_pool_is_big_enough_for_the_sync_threads():
    from livejobs import config, http

    ad = http._session.get_adapter("https://apply.workable.com/")
    assert (
        ad._pool_maxsize >= config.SYNC_WORKERS
    )  # else urllib3 logs "Connection pool is full" under load
