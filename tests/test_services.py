from datetime import timedelta

import pytest
from sqlalchemy import select

from livejobs import db
from livejobs.orm import Application, Board, Job, utcnow
from livejobs.services import (
    ServiceError,
    applications,
    auth,
    boards,
    matching,
    prefs,
    saved,
    sync,
)

from .helpers import fj, mk_board, mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")


# ── auth ────────────────────────────────────────────────────────────────
def test_password_and_token_kinds_are_separate():
    uid = mk_user()
    with db.session() as s:
        u = s.get(auth.User, uid)
        assert (
            auth.authenticate(s, "A@x.io", "correct-horse-battery").id == uid
        )
        assert auth.authenticate(s, "a@x.io", "wrong") is None
        sess, _ = auth.issue_token(s, u, "session")
        mcp, _ = auth.issue_token(s, u, "mcp")
    with db.session() as s:
        assert auth.resolve_token(s, sess, "session").id == uid
        assert (
            auth.resolve_token(s, sess, "mcp") is None
        )  # a session token can't act as an MCP token
        assert (
            auth.resolve_token(s, mcp, "session") is None
        )  # ...and an MCP token can't call the REST API
        assert auth.resolve_token(s, mcp, "mcp").id == uid


def test_token_revoke_expire_and_deactivate():
    uid, other = mk_user(), mk_user("b@x.io")
    with db.session() as s:
        u = s.get(auth.User, uid)
        raw, t = auth.issue_token(s, u, "mcp")
        tid = t.id
        with pytest.raises(ServiceError):  # can't revoke someone else's token
            auth.revoke_token(s, s.get(auth.User, other), tid)
        auth.revoke_token(s, u, tid)
    with db.session() as s:
        assert auth.resolve_token(s, raw, "mcp") is None
        raw2, t2 = auth.issue_token(s, s.get(auth.User, uid), "mcp")
        t2.expires_at = utcnow() - timedelta(seconds=1)
    with db.session() as s:
        assert auth.resolve_token(s, raw2, "mcp") is None
        raw3, _ = auth.issue_token(s, s.get(auth.User, uid), "mcp")
        auth.update_user(s, uid, is_active=False)
    with db.session() as s:
        assert auth.resolve_token(s, raw3, "mcp") is None


def test_last_admin_protected():
    admin = mk_user("root@x.io", "admin")
    with db.session() as s:
        with pytest.raises(ServiceError):
            auth.update_user(s, admin, role="user")
        with pytest.raises(ServiceError):
            auth.update_user(s, admin, is_active=False)
        auth.create_user(s, "r2@x.io", "correct-horse-battery", "admin")
        auth.update_user(s, admin, role="user")  # fine now


# ── boards & sync ───────────────────────────────────────────────────────
def test_crawl_discovered_boards_are_synced_automatically():
    with db.session() as s:
        assert boards.upsert_discovered(s, "lever", {"a": 3, "b": 1}) == 2
        assert boards.upsert_discovered(s, "lever", {"a": 2, "c": 1}) == 1
        assert {
            b.board: (b.hits, b.status) for b in boards.list_boards(s)
        } == {"a": (5, "enabled"), "b": (1, "enabled"), "c": (1, "enabled")}
    assert len(sync.claim_due(10)) == 3  # due immediately, no admin step


def test_a_disabled_board_stays_off_when_crawl_sees_it_again():
    with db.session() as s:
        boards.upsert_discovered(s, "lever", {"a": 1})
        boards.update_board(s, boards.list_boards(s)[0].id, status="disabled")
    with db.session() as s:
        boards.upsert_discovered(s, "lever", {"a": 1})
    assert sync.claim_due(10) == []


def test_enable_discovered_promotes_old_rows_only():
    from livejobs.orm import Board

    with db.session() as s:
        s.add(Board(vendor="lever", board="old", status="discovered"))
        s.add(Board(vendor="lever", board="off", status="disabled"))
    with db.session() as s:
        assert boards.enable_discovered(s) == 1
    assert len(sync.claim_due(10)) == 1


def test_claim_is_a_lease_so_workers_dont_double_sync():
    mk_board()
    assert len(sync.claim_due(10)) == 1
    assert sync.claim_due(10) == []  # leased


def test_sync_upserts_closes_and_reschedules():
    bid = mk_board()
    a, b = fj("Data Engineer"), fj("Designer")
    r = sync.sync_board(bid, lambda v, s: [a, b])
    assert (r["ok"], r["jobs"], r["new"], r["closed"]) == (True, 2, 2, 0)
    r = sync.sync_board(bid, lambda v, s: [a, fj("SRE")])
    assert (r["new"], r["closed"]) == (1, 1)
    with db.session() as s:
        active = {
            j.title for j in s.scalars(select(Job).where(Job.active.is_(True)))
        }
        assert active == {"Data Engineer", "SRE"}
        bd = s.get(Board, bid)
        assert bd.company_name == "Acme" and bd.job_count == 2
        d = bd.next_sync_at - utcnow()
        assert (
            timedelta(minutes=59) < d < timedelta(minutes=67)
        )  # default 60 min interval, up to 10% jitter
        assert (
            s.scalar(
                select(Job).where(Job.title == "Data Engineer")
            ).posted_at.year
            == 2026
        )


def test_per_board_interval_and_failure_backoff():
    bid = mk_board()
    with db.session() as s:
        boards.update_board(s, bid, sync_interval_min=30)
    sync.sync_board(bid, lambda v, s: [])
    with db.session() as s:
        d = s.get(Board, bid).next_sync_at - utcnow()
        assert timedelta(minutes=29) < d < timedelta(minutes=34)

    def boom(v, s):
        raise RuntimeError("down")

    r = sync.sync_board(bid, boom)
    assert not r["ok"] and r["status"] == "enabled"
    with db.session() as s:
        b = s.get(Board, bid)
        assert b.consecutive_failures == 1 and "down" in b.last_error
        assert b.next_sync_at - utcnow() >= timedelta(
            minutes=59
        )  # 30min * 2^1


def test_gone_board_is_disabled_and_jobs_closed():
    from livejobs.vendors.base import BoardGone

    bid = mk_board()
    sync.sync_board(bid, lambda v, s: [fj("X")])

    def gone(v, s):
        raise BoardGone()

    for i in range(3):
        r = sync.sync_board(bid, gone)
    assert r["status"] == "disabled"
    with db.session() as s:
        assert s.scalar(select(Job).where(Job.active.is_(True))) is None


def test_failed_fetch_never_closes_existing_jobs():
    bid = mk_board()
    sync.sync_board(bid, lambda v, s: [fj("Keep Me")])
    sync.sync_board(
        bid, lambda v, s: (_ for _ in ()).throw(RuntimeError("timeout"))
    )
    with db.session() as s:
        assert (
            s.scalar(select(Job).where(Job.active.is_(True))).title
            == "Keep Me"
        )


def test_run_due_end_to_end():
    mk_board("lever", "one")
    mk_board("lever", "two")
    res = sync.run_due(2, fetcher=lambda v, s: [fj("T", board=s)])
    assert len(res) == 2 and all(r["ok"] for r in res)
    assert (
        sync.run_due(2, fetcher=lambda v, s: []) == []
    )  # nothing due until the interval passes


# ── matching ────────────────────────────────────────────────────────────
def _prefs(uid, **kw):
    with db.session() as s:
        return prefs.prefs_dict(
            prefs.update_preferences(s, s.get(auth.User, uid), kw)
        )


def _match(uid, **kw):
    with db.session() as s:
        u = s.get(auth.User, uid)
        return [
            j.title
            for j in matching.find_matches(
                s, uid, prefs.get_preferences(s, u), **kw
            )[0]
        ]


def test_matching_rules():
    uid = mk_user()
    bid = mk_board()
    sync.sync_board(
        bid,
        lambda v, s: [
            fj("Senior Data Engineer", loc="Remote - US"),
            fj("Data Engineering Intern", loc="Remote - US"),
            fj("International Data Engineer", loc="Berlin"),
            fj("Marketing Manager"),
            fj("Data Engineer", company="Evil Corp", loc="Remote"),
            fj("Data Engineer II", desc="requires security clearance"),
        ],
    )
    assert (
        _match(uid) == []
    )  # no title terms -> matches nothing, never everything
    _prefs(uid, title_include=["data engineer"])
    assert set(_match(uid)) == {
        "Senior Data Engineer",
        "International Data Engineer",
        "Data Engineer",
        "Data Engineer II",
    }
    _prefs(uid, title_include=["data engineer*"])  # prefix
    assert "Data Engineering Intern" in _match(uid)
    _prefs(
        uid, title_exclude=["intern"]
    )  # whole word: removes the intern, keeps International
    got = set(_match(uid))
    assert (
        "Data Engineering Intern" not in got
        and "International Data Engineer" in got
    )
    _prefs(
        uid,
        locations=["remote"],
        companies_exclude=["evil"],
        description_exclude=["clearance"],
    )
    assert set(_match(uid)) == {"Senior Data Engineer"}
    _prefs(uid, vendors=["lever"])
    assert _match(uid) == []


def test_matching_is_per_user_and_excludes_ledger_and_disabled_boards():
    u1, u2 = mk_user(), mk_user("b@x.io")
    bid = mk_board()
    sync.sync_board(bid, lambda v, s: [fj("Data Engineer")])
    for u in (u1, u2):
        _prefs(u, title_include=["data engineer"])
    with db.session() as s:
        jid = s.scalar(select(Job.id))
        applications.record(s, s.get(auth.User, u1), jid, "applied")
    assert _match(u1) == [] and _match(u2) == ["Data Engineer"]
    with db.session() as s:
        boards.update_board(s, bid, status="disabled")
    assert _match(u2) == []


def test_pagination_cursor():
    uid = mk_user()
    sync.sync_board(
        mk_board(), lambda v, s: [fj(f"Data Engineer {i}") for i in range(7)]
    )
    _prefs(uid, title_include=["data engineer"])
    with db.session() as s:
        p = prefs.get_preferences(s, s.get(auth.User, uid))
        seen, cur = [], None
        while True:
            jobs, cur = matching.find_matches(s, uid, p, limit=3, after_id=cur)
            seen += [j.title for j in jobs]
            if cur is None:
                break
    assert len(seen) == len(set(seen)) == 7


# ── applications ledger ─────────────────────────────────────────────────
def _setup_job():
    uid = mk_user()
    sync.sync_board(mk_board(), lambda v, s: [fj("Data Engineer"), fj("SRE")])
    with db.session() as s:
        return uid, [j.id for j in s.scalars(select(Job).order_by(Job.id))]


def test_application_lifecycle_and_no_double_apply():
    uid, (j1, j2) = _setup_job()
    with db.session() as s:
        u = s.get(auth.User, uid)
        app, resumed = applications.start(s, u, j1)
        assert app.status == "in_progress" and not resumed
        assert applications.start(s, u, j1)[1] is True  # resume a crashed run
        applications.record(s, u, j1, "applied", "confirmed page")
    with db.session() as s:
        u = s.get(auth.User, uid)
        with pytest.raises(ServiceError, match="already applied"):
            applications.start(s, u, j1)
        with pytest.raises(ServiceError):
            applications.record(
                s, u, j1, "failed"
            )  # can't downgrade an applied job
        a = applications.list_for_user(s, uid)[0]
        assert a.applied_at is not None and a.notes == "confirmed page"


def test_failed_can_be_retried_and_skipped_is_terminal():
    uid, (j1, j2) = _setup_job()
    with db.session() as s:
        u = s.get(auth.User, uid)
        applications.start(s, u, j1)
        applications.record(s, u, j1, "failed", "captcha")
        assert applications.start(s, u, j1)[1] is True
        applications.record(s, u, j2, "skipped")
        with pytest.raises(ServiceError):
            applications.start(s, u, j2)


def test_daily_cap_and_closed_jobs():
    uid, (j1, j2) = _setup_job()
    _prefs(uid, max_applications_per_day=1)
    with db.session() as s:
        u = s.get(auth.User, uid)
        applications.start(s, u, j1)
        with pytest.raises(ServiceError) as e:
            applications.start(s, u, j2)
        assert e.value.code == 429
        s.get(Job, j2).active = False
    with db.session() as s:
        with pytest.raises(ServiceError) as e:
            applications.start(s, s.get(auth.User, uid), j2)
        assert e.value.code == 404


def test_ledger_is_per_user():
    u1, (j1, _) = _setup_job()
    u2 = mk_user("b@x.io")
    with db.session() as s:
        applications.start(s, s.get(auth.User, u1), j1)
        applications.start(
            s, s.get(auth.User, u2), j1
        )  # same job, different user: fine
        assert (
            len(applications.list_for_user(s, u1))
            == len(applications.list_for_user(s, u2))
            == 1
        )
        assert s.scalar(
            select(Application.id).where(
                Application.user_id == u1, Application.job_id == j1
            )
        ) != s.scalar(
            select(Application.id).where(
                Application.user_id == u2, Application.job_id == j1
            )
        )


def test_preferences_validation():
    uid = mk_user()
    with db.session() as s:
        u = s.get(auth.User, uid)
        for bad in (
            {"vendors": ["nope"]},
            {"title_include": "x"},
            {"max_applications_per_day": -1},
            {"full_name": "Ada"},
            {"resume_text": "x"},
        ):  # no personal fields exist to set
            with pytest.raises(ServiceError):
                prefs.update_preferences(s, u, bad)


def test_no_personal_data_is_stored():
    """The privacy promise, as a schema check: no table has a
    name/phone/resume/address-like column."""
    from livejobs.orm import Base

    cols = {c.name for t in Base.metadata.tables.values() for c in t.columns}
    assert not cols & {
        "full_name",
        "phone",
        "resume_text",
        "resume_url",
        "location_text",
        "address",
        "display_name",
        "linkedin_url",
        "answers",
        "cover_letter_template",
    }
    assert "profiles" not in Base.metadata.tables


def test_saved_jobs_lifecycle():
    uid, (j1, j2) = _setup_job()
    other = mk_user("b@x.io")
    with db.session() as s:
        saved.save(s, uid, j1)
        saved.save(s, uid, j1)  # idempotent
        saved.save(s, other, j2)
        with pytest.raises(ServiceError):
            saved.save(s, uid, 99999)
    with db.session() as s:
        assert [j.id for j, _ in saved.list_saved(s, uid)] == [j1]
        s.get(Job, j1).active = False
    with db.session() as s:
        assert saved.list_saved(s, uid) == [
            (s.get(Job, j1), False)
        ]  # closed jobs stay listed, flagged
        saved.unsave(s, uid, j1)
        saved.unsave(s, uid, j1)
        assert saved.saved_ids(s, uid) == [] and saved.saved_ids(s, other) == [
            j2
        ]


def test_change_password_and_delete_account():
    uid, (j1, _) = _setup_job()
    keep = mk_user("keep@x.io")
    with db.session() as s:
        u = s.get(auth.User, uid)
        sess, _ = auth.issue_token(s, u, "session")
        mcp, _ = auth.issue_token(s, u, "mcp")
        with pytest.raises(ServiceError):
            auth.change_password(s, u, "wrong-password-1", "new-password-123")
        auth.change_password(s, u, "correct-horse-battery", "new-password-123")
    with db.session() as s:
        assert (
            auth.resolve_token(s, sess, "session") is None
        )  # sessions end on password change
        assert (
            auth.resolve_token(s, mcp, "mcp") is not None
        )  # a user's agent keeps working
        assert auth.authenticate(s, "a@x.io", "new-password-123")
        u = s.get(auth.User, uid)
        saved.save(s, uid, j1)
        applications.record(s, u, j1, "applied")
        saved.save(s, keep, j1)
        with pytest.raises(ServiceError):
            auth.delete_account(s, u, "wrong")
        auth.delete_account(s, u, "new-password-123")
    with db.session() as s:
        from livejobs.orm import ApiToken, Preferences, SavedJob, User

        assert s.get(User, uid) is None
        for m in (Application, ApiToken, SavedJob, Preferences):
            assert s.scalar(select(m).where(m.user_id == uid)) is None
        assert saved.saved_ids(s, keep) == [j1]  # other users untouched
