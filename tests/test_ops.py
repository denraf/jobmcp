"""Cron-style sync, schema upgrades, SMTP delivery and Prometheus metrics."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from livejobs import config, db, mailer, metrics, migrations
from livejobs.orm import Board, utcnow
from livejobs.services import boards, sync
from livejobs.site import app

from .helpers import fj, mk_board, mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")


# ── hourly cron ─────────────────────────────────────────────────────────
def _set_next(board_id, minutes_from_now):
    with db.session() as s:
        s.get(Board, board_id).next_sync_at = utcnow() + timedelta(
            minutes=minutes_from_now
        )


def test_hourly_cron_needs_slack_or_boards_sync_every_other_hour():
    """A board synced at 10:00 is next due at ~11:00-11:06 (60 min + up to 10%
    jitter). Cron fires at 11:00."""
    bid = mk_board()
    _set_next(bid, 4)  # due in 4 minutes, i.e. just after the cron tick
    assert (
        sync.claim_due(10) == []
    )  # strict: skipped, so it would wait for the *next* tick
    _set_next(bid, 4)
    assert sync.claim_due(10, slack_min=10) == [
        bid
    ]  # with slack: picked up now


def test_slack_does_not_pull_in_boards_that_are_not_due_soon():
    bid = mk_board()
    _set_next(bid, 45)
    assert sync.claim_due(10, slack_min=10) == []


def test_run_due_until_empty_does_not_loop_on_short_intervals():
    """A 5-minute board falls inside a 10-minute slack window again right after
    syncing; it must sync once."""
    bid = mk_board()
    with db.session() as s:
        boards.update_board(s, bid, sync_interval_min=5)
    calls = []
    res = sync.run_due(
        2,
        batch=1,
        fetcher=lambda v, b: calls.append(b) or [fj("T")],
        slack_min=10,
        until_empty=True,
    )
    assert len(res) == 1 and len(calls) == 1


def test_until_empty_drains_more_than_one_batch():
    for i in range(5):
        mk_board("lever", f"b{i}")
    res = sync.run_due(
        2,
        batch=2,
        fetcher=lambda v, b: [fj("T", board=b, vendor="lever")],
        until_empty=True,
    )
    assert len(res) == 5 and all(r["ok"] for r in res)


def test_cli_sync_once_slack_flag(capsys, monkeypatch):
    from livejobs import cli

    mk_board()
    monkeypatch.setattr(
        sync, "default_fetcher", lambda v, b: [fj("Role", board=b)]
    )
    monkeypatch.setattr(
        db, "init", lambda url=None: db.engine()
    )  # keep the test database
    cli.main(["sync-once", "--slack", "10"])
    assert (
        "synced 1 boards: 1 ok, 0 failed, 1 jobs, 1 new"
        in capsys.readouterr().out
    )


# ── schema upgrade ──────────────────────────────────────────────────────
def test_ensure_schema_upgrades_a_pre_verification_database():
    with db.engine().begin() as c:
        c.execute(text("DROP TABLE email_tokens"))
        c.execute(text("ALTER TABLE users DROP COLUMN email_verified_at"))
        c.execute(
            text(
                "INSERT INTO users (email, password_hash, role, is_active, "
                "created_at) "
                "VALUES ('old@x.io', 'x', 'user', 1, '2025-01-02 03:04:05')"
            )
        )
    changed = migrations.ensure_schema()
    assert changed and "email_verified_at" in changed[0]
    with db.engine().connect() as c:
        row = c.execute(
            text(
                "SELECT email_verified_at, created_at FROM users WHERE "
                "email='old@x.io'"
            )
        ).one()
        assert row[0] is not None and str(row[0]) == str(
            row[1]
        )  # grandfathered in, not locked out
        assert (
            c.execute(text("SELECT COUNT(*) FROM email_tokens")).scalar() == 0
        )
    assert migrations.ensure_schema() == []  # idempotent


# ── SMTP ────────────────────────────────────────────────────────────────
class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls, self.sent = host, port, [], None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, u, p):
        self.calls.append(("login", u, p))

    def send_message(self, msg):
        self.sent = msg


def test_smtp_delivery(monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(config, "MAIL_BACKEND", "smtp")
    monkeypatch.setattr(config, "SMTP_HOST", "mail.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "u")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "p")
    monkeypatch.setattr(config, "MAIL_FROM", "livejobs <hello@example.com>")
    FakeSMTP.instances.clear()
    mailer.send(
        mailer.Mail("a@x.io", "Subj", "Body text", "verify"), wait=True
    )
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("mail.example.com", 587)
    assert smtp.calls == [
        "starttls",
        ("login", "u", "p"),
    ]  # encrypted before credentials go over the wire
    assert (
        smtp.sent["To"] == "a@x.io"
        and smtp.sent["From"] == "livejobs <hello@example.com>"
    )
    assert (
        smtp.sent["Subject"] == "Subj"
        and "Body text" in smtp.sent.get_content()
    )


def test_smtp_failure_is_counted_not_raised(monkeypatch):
    class Boom(FakeSMTP):
        def send_message(self, msg):
            raise OSError("relay down")

    monkeypatch.setattr(mailer.smtplib, "SMTP", Boom)
    monkeypatch.setattr(config, "MAIL_BACKEND", "smtp")
    before = metrics.EMAILS.labels("verify", "failed")._value.get()
    mailer.send(
        mailer.Mail("a@x.io", "S", "B", "verify"), wait=True
    )  # must not raise into the request
    assert metrics.EMAILS.labels("verify", "failed")._value.get() == before + 1


def test_signup_still_succeeds_when_mail_fails(monkeypatch):
    class Boom(FakeSMTP):
        def send_message(self, msg):
            raise OSError("relay down")

    monkeypatch.setattr(mailer.smtplib, "SMTP", Boom)
    monkeypatch.setattr(config, "MAIL_BACKEND", "smtp")
    r = TestClient(app).post(
        "/api/auth/register",
        json={"email": "a@x.io", "password": "correct-horse-battery"},
    )
    assert r.status_code == 202


# ── metrics ─────────────────────────────────────────────────────────────
def scrape(client=None, headers=None):
    return (client or TestClient(app, client=("127.0.0.1", 5000))).get(
        "/metrics", headers=headers or {}
    )


def sample(body, name, labels=""):
    m = re.search(
        rf"^{re.escape(name)}{re.escape(labels)} ([0-9.e+-]+)$", body, re.M
    )
    return float(m.group(1)) if m else None


def test_metrics_report_the_database_state():
    mk_user("a@x.io")
    mk_user("admin@x.io", "admin")
    with db.session() as s:
        from livejobs.services import auth

        auth.register(s, "pending@x.io", "correct-horse-battery")
    b1, b2 = mk_board("greenhouse", "acme"), mk_board("lever", "beta")
    sync.sync_board(b1, lambda v, s: [fj("A"), fj("B")])
    sync.sync_board(
        b2, lambda v, s: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with db.session() as s:
        s.add(Board(vendor="ashby", board="x", status="discovered"))
    r = scrape()
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "text/plain"
    )
    t = r.text
    assert sample(t, "livejobs_db_up") == 1
    assert sample(t, "livejobs_users", '{role="user",verified="true"}') == 1
    assert sample(t, "livejobs_users", '{role="user",verified="false"}') == 1
    assert sample(t, "livejobs_users", '{role="admin",verified="true"}') == 1
    assert (
        sample(t, "livejobs_boards", '{status="enabled",vendor="greenhouse"}')
        == 1
    )
    assert (
        sample(t, "livejobs_boards", '{status="discovered",vendor="ashby"}')
        == 1
    )
    assert sample(t, "livejobs_boards_failing", '{vendor="lever"}') == 1
    assert sample(t, "livejobs_jobs_active", '{vendor="greenhouse"}') == 2
    assert sample(t, "livejobs_sync_runs_total", '{outcome="ok"}') == 1
    assert sample(t, "livejobs_sync_runs_total", '{outcome="error"}') == 1
    assert sample(t, "livejobs_sync_jobs_new_total") == 2
    assert (
        sample(t, "livejobs_sync_last_success_timestamp_seconds") > 1.7e9
    )  # a real epoch, not 0
    assert sample(t, "livejobs_boards_overdue") == 0


def test_overdue_boards_are_reported():
    bid = mk_board()
    _set_next(bid, -90)
    assert sample(scrape().text, "livejobs_boards_overdue") == 1


def test_in_process_counters_and_bounded_labels():
    c = TestClient(app, client=("127.0.0.1", 5000))
    mk_user("a@x.io")
    c.post("/api/auth/login", json={"email": "a@x.io", "password": "wrong"})
    c.post(
        "/api/auth/login",
        json={"email": "a@x.io", "password": "correct-horse-battery"},
    )
    c.get("/api/public/jobs/987654")  # a 404 on a templated route
    c.get("/definitely/not/a/page")
    t = scrape(c).text
    assert (
        'livejobs_auth_events_total{event="login_failed"}' in t
        and 'livejobs_auth_events_total{event="login"}' in t
    )
    assert (
        'handler="/api/public/jobs/{job_id}"' in t and "987654" not in t
    )  # route template, never the raw id
    assert 'handler="unmatched"' in t and "definitely" not in t
    assert "livejobs_http_request_duration_seconds_bucket" in t


def test_metrics_access_rules(monkeypatch):
    mk_user("a@x.io")
    outside = TestClient(app)  # client host "testclient": not loopback
    assert scrape(outside).status_code == 403
    assert scrape().status_code == 200  # loopback, direct
    assert (
        scrape(headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403
    )  # loopback *via a proxy* is not trusted
    monkeypatch.setattr(config, "METRICS_PUBLIC", True)
    assert scrape(outside).status_code == 200
    monkeypatch.setattr(config, "METRICS_PUBLIC", False)
    monkeypatch.setattr(config, "METRICS_TOKEN", "s3cret-token")
    assert (
        scrape().status_code == 403
    )  # with a token set, loopback alone is no longer enough
    assert scrape(headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert (
        scrape(
            outside, headers={"Authorization": "Bearer s3cret-token"}
        ).status_code
        == 200
    )
    assert (
        scrape(
            headers={
                "X-Forwarded-For": "203.0.113.9",
                "Authorization": "Bearer s3cret-token",
            }
        ).status_code
        == 200
    )


def test_metrics_survive_a_database_outage(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(metrics, "_snapshot", boom)
    metrics.collector.reset()
    r = scrape()
    assert (
        r.status_code == 200 and sample(r.text, "livejobs_db_up") == 0
    )  # the scrape works, and says the DB is down
    assert (
        "livejobs_http_requests_total" in r.text
    )  # in-process metrics are still served


def test_send_test_email_command_reports_smtp_failure(monkeypatch):
    from livejobs import cli

    class Boom(FakeSMTP):
        def send_message(self, msg):
            raise OSError("relay down")

    monkeypatch.setattr(mailer.smtplib, "SMTP", Boom)
    monkeypatch.setattr(config, "MAIL_BACKEND", "smtp")
    monkeypatch.setattr(db, "init", lambda url=None: db.engine())
    with pytest.raises(SystemExit) as e:
        cli.main(["send-test-email", "--to", "a@x.io"])
    assert "did not accept" in str(
        e.value
    )  # non-zero exit, so a setup script or a human notices
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.instances.clear()
    cli.main(["send-test-email", "--to", "a@x.io"])
    assert FakeSMTP.instances[0].sent["To"] == "a@x.io"
