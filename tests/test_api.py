import pytest
from fastapi.testclient import TestClient

from livejobs import db
from livejobs.api import app
from livejobs.services import auth, sync

from .helpers import fj, mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")
PW = "correct-horse-battery"


@pytest.fixture()
def c():
    return TestClient(app)


def login(c, email):
    r = c.post("/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_login_and_auth_required(c):
    mk_user("a@x.io")
    assert (
        c.post(
            "/auth/login", json={"email": "a@x.io", "password": "bad"}
        ).status_code
        == 401
    )
    assert c.get("/me").status_code == 401
    assert (
        c.get("/me", headers={"Authorization": "Bearer junk"}).status_code
        == 401
    )
    h = login(c, "a@x.io")
    assert c.get("/me", headers=h).json()["email"] == "a@x.io"
    assert c.post("/auth/logout", headers=h).status_code == 204
    assert c.get("/me", headers=h).status_code == 401


def test_roles_admin_endpoints_forbidden_to_users(c):
    mk_user("admin@x.io", "admin")
    mk_user("u@x.io")
    uh = login(c, "u@x.io")
    for method, path in [
        ("get", "/admin/users"),
        ("get", "/admin/boards"),
        ("get", "/admin/stats"),
        ("post", "/admin/boards/bulk"),
    ]:
        assert getattr(c, method)(path, headers=uh).status_code == 403, path
    ah = login(c, "admin@x.io")
    assert c.get("/admin/users", headers=ah).status_code == 200


def test_admin_manages_users(c):
    mk_user("admin@x.io", "admin")
    ah = login(c, "admin@x.io")
    r = c.post(
        "/admin/users", headers=ah, json={"email": "new@x.io", "password": PW}
    )
    assert r.status_code == 201 and r.json()["role"] == "user"
    assert (
        c.post(
            "/admin/users",
            headers=ah,
            json={"email": "new@x.io", "password": PW},
        ).status_code
        == 409
    )
    assert (
        c.post(
            "/admin/users",
            headers=ah,
            json={"email": "w@x.io", "password": "short"},
        ).status_code
        == 400
    )
    uid = r.json()["id"]
    assert (
        c.patch(
            f"/admin/users/{uid}", headers=ah, json={"is_active": False}
        ).json()["is_active"]
        is False
    )
    assert (
        c.post(
            "/auth/login", json={"email": "new@x.io", "password": PW}
        ).status_code
        == 401
    )  # deactivated


def test_admin_controls_slugs(c, monkeypatch):
    monkeypatch.setattr(
        sync, "default_fetcher", lambda v, b: [fj("Role", board=b)]
    )
    mk_user("admin@x.io", "admin")
    ah = login(c, "admin@x.io")
    with db.session() as s:
        from livejobs.services import boards

        boards.upsert_discovered(s, "greenhouse", {"big": 50, "small": 1})
    listed = c.get("/admin/boards?status=enabled", headers=ah).json()
    assert [b["board"] for b in listed] == [
        "big",
        "small",
    ]  # most-referenced first
    assert (
        len(sync.claim_due(10)) == 2
    )  # found boards sync without an admin step

    assert c.post(
        "/admin/boards/bulk",
        headers=ah,
        json={
            "vendor": "greenhouse",
            "from_status": "enabled",
            "to_status": "disabled",
            "min_hits": 10,
        },
    ).json() == {"updated": 1}
    bid = listed[0]["id"]
    r = c.patch(
        f"/admin/boards/{bid}",
        headers=ah,
        json={"status": "enabled", "sync_interval_min": 30},
    ).json()
    assert r["status"] == "enabled" and r["sync_interval_min"] == 30
    assert (
        c.patch(
            f"/admin/boards/{bid}", headers=ah, json={"sync_interval_min": 1}
        ).status_code
        == 400
    )

    added = c.post(
        "/admin/boards", headers=ah, json={"vendor": "lever", "slug": "manual"}
    ).json()
    assert added["status"] == "enabled"
    assert (
        c.post(
            "/admin/boards", headers=ah, json={"vendor": "nope", "slug": "x"}
        ).status_code
        == 400
    )

    r = c.post(f"/admin/boards/{bid}/sync", headers=ah)
    assert r.status_code == 200 and r.json()["board"] == "big"
    assert c.get("/admin/sync-runs", headers=ah).json()[0]["board_id"] == bid
    c.patch(f"/admin/boards/{bid}", headers=ah, json={"status": "disabled"})
    assert bid not in sync.claim_due(10)


def test_user_preferences_matches_and_mcp_token_lifecycle(c):
    mk_user("u@x.io")
    h = login(c, "u@x.io")
    sync.sync_board(
        __import__("tests.helpers", fromlist=["x"]).mk_board(),
        lambda v, s: [fj("Data Engineer"), fj("Chef")],
    )

    assert c.put(
        "/me/preferences", headers=h, json={"title_include": ["data engineer"]}
    ).json()["title_include"] == ["data engineer"]
    assert (
        c.put(
            "/me/preferences", headers=h, json={"vendors": ["bogus"]}
        ).status_code
        == 400
    )
    assert (
        c.get("/me/profile", headers=h).status_code == 404
    )  # there is no profile to store

    m = c.get("/me/matches", headers=h).json()
    assert [j["title"] for j in m["jobs"]] == ["Data Engineer"]
    jid = m["jobs"][0]["id"]
    assert c.get(f"/jobs/{jid}", headers=h).json()["title"] == "Data Engineer"

    # user applied by hand -> recorded, no longer matched
    assert (
        c.put(
            f"/me/applications/{jid}", headers=h, json={"status": "applied"}
        ).json()["method"]
        == "manual"
    )
    assert c.get("/me/matches", headers=h).json()["jobs"] == []
    assert (
        c.get("/me/applications", headers=h).json()[0]["status"] == "applied"
    )

    t = c.post("/me/mcp-tokens", headers=h, json={"name": "claude"})
    assert t.status_code == 201 and t.json()["token"].startswith("lj_m_")
    raw = t.json()["token"]
    # an MCP token must not open the REST API
    assert (
        c.get("/me", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 401
    )
    listing = c.get("/me/mcp-tokens", headers=h).json()
    assert (
        len(listing) == 1
        and "token" not in listing[0]
        and listing[0]["name"] == "claude"
    )
    assert (
        c.delete(f"/me/mcp-tokens/{listing[0]['id']}", headers=h).status_code
        == 204
    )
    assert c.get("/me/mcp-tokens", headers=h).json() == []


def test_users_cannot_touch_each_others_tokens(c):
    mk_user("a@x.io")
    mk_user("b@x.io")
    ha, hb = login(c, "a@x.io"), login(c, "b@x.io")
    tid = c.post("/me/mcp-tokens", headers=ha, json={}).json()["id"]
    assert c.delete(f"/me/mcp-tokens/{tid}", headers=hb).status_code == 404
    assert len(c.get("/me/mcp-tokens", headers=ha).json()) == 1


def test_search_endpoint(c):
    from .helpers import mk_board

    mk_user("u@x.io")
    h = login(c, "u@x.io")
    sync.sync_board(
        mk_board(),
        lambda v, s: [
            fj("Data Engineer", desc="We love Airflow."),
            fj("Chef", desc="Airflow in the kitchen"),
        ],
    )
    assert c.get("/jobs/search", headers=h).status_code == 400  # needs a query
    assert c.get("/jobs/search?title=data", headers={}).status_code == 401
    r = c.get("/jobs/search?keywords=airflow", headers=h).json()
    assert (
        r["total"] == 2
        and "Airflow" in r["hits"][0]["snippet"]
        and r["hits"][0]["my_status"] is None
    )
    r = c.get(
        "/jobs/search?title=data%20engineer&keywords=airflow", headers=h
    ).json()
    assert [x["title"] for x in r["hits"]] == ["Data Engineer"] and r["hits"][
        0
    ]["title_match"] is True
    jid = r["hits"][0]["id"]
    assert (
        c.get(f"/jobs/{jid}", headers=h).json()["title"] == "Data Engineer"
    )  # /jobs/search didn't shadow /jobs/{id}


def test_register_requires_verification_and_does_not_leak_accounts(
    c, monkeypatch
):
    from livejobs import config, mailer

    r = c.post("/auth/register", json={"email": "New@X.io", "password": PW})
    assert (
        r.status_code == 202
        and r.json()["status"] == "check_email"
        and "token" not in r.json()
    )
    assert (
        len(mailer.outbox) == 1
        and mailer.outbox[0].to == "new@x.io"
        and mailer.outbox[0].purpose == "verify"
    )
    # cannot sign in until the link is used, and the message says why
    bad = c.post("/auth/login", json={"email": "new@x.io", "password": PW})
    assert (
        bad.status_code == 403 and "Confirm your email" in bad.json()["detail"]
    )
    # the identical answer for an address that already exists (no account
    # enumeration)
    mk_user("taken@x.io")
    r2 = c.post("/auth/register", json={"email": "taken@x.io", "password": PW})
    assert r2.status_code == 202 and r2.json() == r.json()
    assert (
        mailer.outbox[-1].purpose == "already_registered"
        and mailer.outbox[-1].to == "taken@x.io"
    )
    assert (
        c.post(
            "/auth/register", json={"email": "bad", "password": PW}
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/auth/register", json={"email": "s@x.io", "password": "short"}
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/auth/register",
            json={"email": "r@x.io", "password": PW, "role": "admin"},
        ).status_code
        == 202
    )
    monkeypatch.setattr(config, "ALLOW_REGISTRATION", False)
    assert (
        c.post(
            "/auth/register", json={"email": "z@x.io", "password": PW}
        ).status_code
        == 403
    )
    assert c.get("/public/config").json()["registration_open"] is False


def _link_token(mail):
    import re

    return re.search(r"token=([\w-]+)", mail.body).group(1)


def test_verify_email_flow(c):
    from livejobs import mailer

    c.post("/auth/register", json={"email": "v@x.io", "password": PW})
    tok = _link_token(mailer.outbox[-1])
    assert "https://jobs.test/verify?token=" in mailer.outbox[-1].body
    assert (
        c.post("/auth/verify", json={"token": "nonsense"}).status_code == 400
    )
    assert c.post("/auth/verify", json={"token": tok}).json() == {
        "verified": True
    }
    assert (
        c.post("/auth/verify", json={"token": tok}).status_code == 400
    )  # single use
    assert (
        c.post(
            "/auth/login", json={"email": "v@x.io", "password": PW}
        ).status_code
        == 200
    )


def test_resend_verification_cooldown_and_silence(c):
    from livejobs import mailer
    from livejobs.orm import EmailToken

    c.post("/auth/register", json={"email": "v@x.io", "password": PW})
    assert len(mailer.outbox) == 1
    assert (
        c.post(
            "/auth/resend-verification", json={"email": "v@x.io"}
        ).status_code
        == 202
    )
    assert len(mailer.outbox) == 1  # inside the cooldown: no second email
    with db.session() as s:  # pretend the first email is old
        for t in s.query(EmailToken):
            t.created_at = t.created_at.replace(year=2020)
    first = _link_token(mailer.outbox[0])
    assert (
        c.post(
            "/auth/resend-verification", json={"email": "v@x.io"}
        ).status_code
        == 202
    )
    assert len(mailer.outbox) == 2
    assert (
        c.post("/auth/verify", json={"token": first}).status_code == 400
    )  # the old link was replaced
    assert (
        c.post(
            "/auth/verify", json={"token": _link_token(mailer.outbox[1])}
        ).status_code
        == 200
    )
    # unknown / already-verified addresses: same reply, no email
    n = len(mailer.outbox)
    assert (
        c.post(
            "/auth/resend-verification", json={"email": "nobody@x.io"}
        ).status_code
        == 202
    )
    assert (
        c.post(
            "/auth/resend-verification", json={"email": "v@x.io"}
        ).status_code
        == 202
    )
    assert len(mailer.outbox) == n


def test_expired_verification_link(c):
    from livejobs import mailer
    from livejobs.orm import EmailToken

    c.post("/auth/register", json={"email": "v@x.io", "password": PW})
    with db.session() as s:
        for t in s.query(EmailToken):
            t.expires_at = t.expires_at.replace(year=2020)
    assert (
        c.post(
            "/auth/verify", json={"token": _link_token(mailer.outbox[-1])}
        ).status_code
        == 400
    )


def test_signup_again_before_verifying_replaces_the_password(c):
    from livejobs import mailer
    from livejobs.orm import EmailToken

    c.post(
        "/auth/register",
        json={"email": "v@x.io", "password": "first-password-1"},
    )
    with db.session() as s:
        for t in s.query(EmailToken):
            t.created_at = t.created_at.replace(year=2020)
    c.post(
        "/auth/register",
        json={"email": "v@x.io", "password": "second-password-2"},
    )
    c.post("/auth/verify", json={"token": _link_token(mailer.outbox[-1])})
    assert (
        c.post(
            "/auth/login",
            json={"email": "v@x.io", "password": "first-password-1"},
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/auth/login",
            json={"email": "v@x.io", "password": "second-password-2"},
        ).status_code
        == 200
    )


def test_password_reset_flow(c):
    from livejobs import mailer

    mk_user("u@x.io")
    h = login(c, "u@x.io")
    mcp = c.post("/me/mcp-tokens", headers=h, json={}).json()["token"]
    # unknown address: same answer, no email
    assert (
        c.post("/auth/forgot", json={"email": "ghost@x.io"}).status_code == 202
        and not mailer.outbox
    )
    assert c.post("/auth/forgot", json={"email": "U@x.io"}).status_code == 202
    mail = mailer.outbox[-1]
    assert (
        mail.purpose == "reset"
        and mail.to == "u@x.io"
        and "/reset?token=" in mail.body
    )
    tok = _link_token(mail)
    assert (
        c.post(
            "/auth/reset", json={"token": tok, "password": "short"}
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/auth/reset",
            json={"token": "nope", "password": "brand-new-password"},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/auth/reset",
            json={"token": tok, "password": "brand-new-password"},
        ).status_code
        == 204
    )  # the short try didn't burn it
    assert (
        c.post(
            "/auth/reset",
            json={"token": tok, "password": "another-password-1"},
        ).status_code
        == 400
    )  # single use
    assert c.get("/me", headers=h).status_code == 401  # old session ended
    assert (
        c.post(
            "/auth/login", json={"email": "u@x.io", "password": PW}
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/auth/login",
            json={"email": "u@x.io", "password": "brand-new-password"},
        ).status_code
        == 200
    )
    with db.session() as s:
        assert (
            auth.resolve_token(s, mcp, "mcp") is not None
        )  # the user's agent is unaffected


def test_reset_link_expires_and_new_request_replaces_old(c):
    from livejobs import mailer
    from livejobs.orm import EmailToken

    mk_user("u@x.io")
    c.post("/auth/forgot", json={"email": "u@x.io"})
    old = _link_token(mailer.outbox[-1])
    with db.session() as s:
        for t in s.query(EmailToken):
            t.created_at = t.created_at.replace(year=2020)
    c.post("/auth/forgot", json={"email": "u@x.io"})
    new = _link_token(mailer.outbox[-1])
    assert (
        c.post(
            "/auth/reset",
            json={"token": old, "password": "brand-new-password"},
        ).status_code
        == 400
    )
    with db.session() as s:
        for t in s.query(EmailToken):
            t.expires_at = t.expires_at.replace(year=2020)
    assert (
        c.post(
            "/auth/reset",
            json={"token": new, "password": "brand-new-password"},
        ).status_code
        == 400
    )


def test_reset_also_confirms_an_unverified_address(c):
    from livejobs import mailer

    c.post("/auth/register", json={"email": "v@x.io", "password": PW})
    c.post("/auth/forgot", json={"email": "v@x.io"})
    reset = [m for m in mailer.outbox if m.purpose == "reset"][-1]
    assert (
        c.post(
            "/auth/reset",
            json={
                "token": _link_token(reset),
                "password": "brand-new-password",
            },
        ).status_code
        == 204
    )
    assert (
        c.post(
            "/auth/login",
            json={"email": "v@x.io", "password": "brand-new-password"},
        ).status_code
        == 200
    )


def test_verification_can_be_switched_off(c, monkeypatch):
    from livejobs import config, mailer

    monkeypatch.setattr(config, "REQUIRE_EMAIL_VERIFICATION", False)
    r = c.post("/auth/register", json={"email": "n@x.io", "password": PW})
    assert r.status_code == 201 and r.json()["token"] and not mailer.outbox
    assert (
        c.get(
            "/me", headers={"Authorization": f"Bearer {r.json()['token']}"}
        ).status_code
        == 200
    )


def test_unverified_user_cannot_use_a_pre_existing_session(c):
    """Admin-created and CLI users are verified; only self-signups start
    unverified."""
    mk_user("admin@x.io", "admin")
    ah = login(c, "admin@x.io")
    r = c.post(
        "/admin/users", headers=ah, json={"email": "made@x.io", "password": PW}
    )
    assert r.status_code == 201
    assert (
        c.post(
            "/auth/login", json={"email": "made@x.io", "password": PW}
        ).status_code
        == 200
    )


def test_auth_rate_limit(c, monkeypatch):
    from livejobs import config

    monkeypatch.setattr(config, "AUTH_RATE_PER_MIN", 3)
    codes = [
        c.post(
            "/auth/login", json={"email": "no@x.io", "password": "x"}
        ).status_code
        for _ in range(5)
    ]
    assert codes == [401, 401, 401, 429, 429]


def test_password_change_and_account_deletion(c):
    mk_user("u@x.io")
    h = login(c, "u@x.io")
    assert (
        c.put(
            "/me/password",
            headers=h,
            json={
                "current_password": "nope",
                "new_password": "another-password-1",
            },
        ).status_code
        == 403
    )
    assert (
        c.put(
            "/me/password",
            headers=h,
            json={
                "current_password": PW,
                "new_password": "another-password-1",
            },
        ).status_code
        == 204
    )
    assert c.get("/me", headers=h).status_code == 401  # old session ended
    r = c.post(
        "/auth/login",
        json={"email": "u@x.io", "password": "another-password-1"},
    )
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert (
        c.post("/me/delete", headers=h, json={"password": "wrong"}).status_code
        == 403
    )
    assert (
        c.post(
            "/me/delete", headers=h, json={"password": "another-password-1"}
        ).status_code
        == 204
    )
    assert (
        c.post(
            "/auth/login",
            json={"email": "u@x.io", "password": "another-password-1"},
        ).status_code
        == 401
    )


def test_saved_jobs_endpoints(c):
    from .helpers import mk_board

    mk_user("u@x.io")
    mk_user("o@x.io")
    h, ho = login(c, "u@x.io"), login(c, "o@x.io")
    sync.sync_board(mk_board(), lambda v, s: [fj("Data Engineer")])
    jid = c.get("/public/jobs").json()["hits"][0]["id"]
    assert c.put(f"/me/saved/{jid}", headers=h).status_code == 204
    assert c.put(f"/me/saved/{jid}", headers=h).status_code == 204
    assert c.put("/me/saved/9999", headers=h).status_code == 404
    assert c.get("/me/saved/ids", headers=h).json() == [jid]
    assert [j["title"] for j in c.get("/me/saved", headers=h).json()] == [
        "Data Engineer"
    ]
    assert c.get("/me/saved", headers=ho).json() == []  # private to the user
    assert c.get("/me/saved").status_code == 401
    assert c.delete(f"/me/saved/{jid}", headers=h).status_code == 204
    assert c.get("/me/saved/ids", headers=h).json() == []


def test_public_jobs_and_stats(c):
    from .helpers import mk_board

    sync.sync_board(
        mk_board(),
        lambda v, s: [
            fj("Data Engineer", desc="Airflow pipelines", loc="Remote - US"),
            fj("Chef", desc="Kitchen", loc="Paris"),
        ],
    )
    sync.sync_board(
        mk_board("lever", "beta"),
        lambda v, s: [fj("SRE", board="beta", vendor="lever", loc="Berlin")],
    )
    assert c.get("/public/stats").json() == {
        "open_jobs": 3,
        "companies": 2,
        "platforms": 2,
    }
    r = c.get("/public/jobs").json()  # no login, no query: browse
    assert r["total"] == 3 and [h["title"] for h in r["hits"]] == [
        "SRE",
        "Chef",
        "Data Engineer",
    ]  # newest first
    assert all(
        "description" not in h and "my_status" in h and h["my_status"] is None
        for h in r["hits"]
    )
    assert c.get("/public/jobs?vendor=lever").json()["total"] == 1
    assert c.get("/public/jobs?remote_only=true").json()["total"] == 1
    assert (
        c.get("/public/jobs?location=paris").json()["hits"][0]["title"]
        == "Chef"
    )
    k = c.get("/public/jobs?keywords=airflow").json()
    assert k["total"] == 1 and "Airflow" in k["hits"][0]["snippet"]
    p = c.get("/public/jobs?limit=2").json()
    assert len(p["hits"]) == 2 and p["next_offset"] == 2
    assert c.get("/public/jobs?limit=500").status_code == 422
    jid = k["hits"][0]["id"]
    assert (
        c.get(f"/public/jobs/{jid}").json()["description"]
        == "Airflow pipelines"
    )
    assert c.get("/public/jobs/99999").status_code == 404


def test_public_stats_work_when_the_machine_has_only_just_booted(
    c, monkeypatch
):
    """time.monotonic() counts from boot. A fresh container VM has been up for
    seconds, and the first stats call
    used to return null because 'now - 0.0' was under the 60 s cache
    lifetime."""
    import time

    from livejobs import api

    monkeypatch.setitem(api._stats_cache, "at", None)
    monkeypatch.setitem(api._stats_cache, "v", None)
    monkeypatch.setattr(time, "monotonic", lambda: 7.0)  # up for 7 seconds
    r = c.get("/public/stats")
    assert r.status_code == 200 and r.json() == {
        "open_jobs": 0,
        "companies": 0,
        "platforms": 0,
    }
