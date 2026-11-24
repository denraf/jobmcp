"""Agents sign themselves in: dynamic registration, authorization code with
PKCE, refresh rotation, and the pasted tokens still working next to it."""

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from jobmcp import config, mcp_server
from jobmcp.site import app

from .helpers import mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")

REDIRECT = "http://127.0.0.1:33418/callback"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b"=")
    .decode()
)


@pytest.fixture()
def c():
    return TestClient(app)


def session(c, email="a@x.io"):
    mk_user(email)
    r = c.post(
        "/api/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    return {"Authorization": "Bearer " + r.json()["token"]}


def register(c, **kw):
    body = {
        "client_name": "Test Agent",
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        **kw,
    }
    return c.post("/oauth/register", json=body)


def authorize_params(client_id, **kw):
    return {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "xyz",
        **kw,
    }


def sign_in(c, headers, **kw):
    cid = register(c).json()["client_id"]
    r = c.post(
        "/api/oauth/decision",
        headers=headers,
        json={**authorize_params(cid, **kw), "approve": True},
    )
    assert r.status_code == 200, r.text
    q = parse_qs(urlsplit(r.json()["redirect_to"]).query)
    assert q["state"] == ["xyz"] and q["iss"] == [config.PUBLIC_BASE_URL]
    return cid, q["code"][0]


def token(c, **form):
    return c.post(
        "/oauth/token",
        content=urlencode(form),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def exchange(c, cid, code, **kw):
    return token(
        c,
        grant_type="authorization_code",
        client_id=cid,
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=VERIFIER,
        **kw,
    )


def test_discovery_documents(c):
    r = c.get("/.well-known/oauth-protected-resource")
    assert r.json()["authorization_servers"] == [config.PUBLIC_BASE_URL]
    assert r.headers["access-control-allow-origin"] == "*"
    m = c.get("/.well-known/oauth-authorization-server").json()
    assert m["code_challenge_methods_supported"] == ["S256"]
    assert m["registration_endpoint"].endswith("/oauth/register")
    # the 401 an unauthenticated agent gets points at the metadata
    import asyncio

    sent = []

    async def go():
        async def send(msg):
            sent.append(msg)

        async def receive():
            return {}

        await mcp_server.BearerAuth(None)(
            {"type": "http", "headers": [], "path": "/mcp/"}, receive, send
        )

    asyncio.run(go())
    hdrs = dict(sent[0]["headers"])
    assert sent[0]["status"] == 401
    assert b"resource_metadata=" in hdrs[b"www-authenticate"]


def test_registration_refuses_unsafe_redirects(c):
    for bad in (
        "http://evil.example/cb",  # plain http off this machine
        "javascript:alert(1)",
        "https://user:pw@x.example/cb",
        "https://x.example/cb#frag",
    ):
        r = register(c, redirect_uris=[bad])
        assert r.status_code == 400, bad
        assert r.json()["error"] == "invalid_redirect_uri"
    assert (
        register(
            c, token_endpoint_auth_method="client_secret_basic"
        ).status_code
        == 400
    )
    ok = register(c, redirect_uris=["https://app.example/cb", "cursor://x/y"])
    assert ok.status_code == 201 and ok.json()["client_id"].startswith("jmc_")


def test_full_sign_in_then_the_agent_uses_mcp_and_refreshes(c):
    h = session(c)
    cid, code = sign_in(c, h)
    r = exchange(c, cid, code)
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["token_type"] == "Bearer" and t["expires_in"] > 0
    # the access token is a normal MCP token...
    from jobmcp import db
    from jobmcp.services import auth

    with db.session() as s:
        assert auth.resolve_token(s, t["access_token"], "mcp") is not None
        # ...and not a REST session
        assert auth.resolve_token(s, t["access_token"], "session") is None
    # refresh rotates
    r2 = token(
        c,
        grant_type="refresh_token",
        client_id=cid,
        refresh_token=t["refresh_token"],
    )
    assert r2.status_code == 200
    # replaying the old refresh token kills the whole sign-in
    r3 = token(
        c,
        grant_type="refresh_token",
        client_id=cid,
        refresh_token=t["refresh_token"],
    )
    assert r3.status_code == 400
    r4 = token(
        c,
        grant_type="refresh_token",
        client_id=cid,
        refresh_token=r2.json()["refresh_token"],
    )
    assert r4.status_code == 400
    with db.session() as s:
        assert auth.resolve_token(s, r2.json()["access_token"], "mcp") is None


def test_a_code_works_once_and_replaying_it_revokes_what_it_gave(c):
    from jobmcp import db
    from jobmcp.services import auth

    h = session(c)
    cid, code = sign_in(c, h)
    t = exchange(c, cid, code).json()
    assert exchange(c, cid, code).status_code == 400
    with db.session() as s:
        assert auth.resolve_token(s, t["access_token"], "mcp") is None


def test_pkce_and_redirect_are_enforced(c):
    h = session(c)
    cid, code = sign_in(c, h)
    r = token(
        c,
        grant_type="authorization_code",
        client_id=cid,
        code=code,
        redirect_uri=REDIRECT,
        code_verifier="x" * 64,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    cid, code = sign_in(c, h)
    r = token(
        c,
        grant_type="authorization_code",
        client_id=cid,
        code=code,
        redirect_uri="http://127.0.0.1:1/other",
        code_verifier=VERIFIER,
    )
    assert r.status_code == 400
    # an unregistered redirect never reaches a decision
    r = c.post(
        "/api/oauth/decision",
        headers=h,
        json={
            **authorize_params(cid, redirect_uri="https://evil.example/"),
            "approve": True,
        },
    )
    assert r.status_code == 400
    # no PKCE, no code
    p = authorize_params(cid)
    p.pop("code_challenge")
    assert (
        c.post(
            "/api/oauth/decision", headers=h, json={**p, "approve": True}
        ).status_code
        == 400
    )


def test_loopback_port_may_differ_and_deny_returns_access_denied(c):
    h = session(c)
    cid = register(c).json()["client_id"]
    p = authorize_params(cid, redirect_uri="http://127.0.0.1:55555/callback")
    r = c.post("/api/oauth/decision", headers=h, json={**p, "approve": False})
    q = parse_qs(urlsplit(r.json()["redirect_to"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["xyz"]


def test_wrong_resource_is_refused(c):
    h = session(c)
    cid = register(c).json()["client_id"]
    r = c.post(
        "/api/oauth/decision",
        headers=h,
        json={
            **authorize_params(cid, resource="https://other.example/mcp"),
            "approve": True,
        },
    )
    assert r.status_code == 400


def test_user_sees_and_revokes_connected_apps_and_tokens_stay_separate(c):
    h = session(c)
    cid, code = sign_in(c, h)
    t = exchange(c, cid, code).json()
    apps = c.get("/api/me/connected-apps", headers=h).json()
    assert [a["name"] for a in apps] == ["Test Agent"]
    # signed-in access tokens are not in the list of pasted tokens
    assert c.get("/api/me/mcp-tokens", headers=h).json() == []
    # a pasted token still works exactly as before
    pasted = c.post(
        "/api/me/mcp-tokens", headers=h, json={"name": "mine"}
    ).json()
    from jobmcp import db
    from jobmcp.services import auth

    with db.session() as s:
        assert auth.resolve_token(s, pasted["token"], "mcp") is not None
    assert (
        c.delete(
            f"/api/me/connected-apps/{apps[0]['id']}", headers=h
        ).status_code
        == 204
    )
    with db.session() as s:
        assert auth.resolve_token(s, t["access_token"], "mcp") is None
        assert auth.resolve_token(s, pasted["token"], "mcp") is not None
    assert (
        token(
            c,
            grant_type="refresh_token",
            client_id=cid,
            refresh_token=t["refresh_token"],
        ).status_code
        == 400
    )
    # another user cannot disconnect it
    other = session(c, "b@x.io")
    assert (
        c.delete(
            f"/api/me/connected-apps/{apps[0]['id']}", headers=other
        ).status_code
        == 404
    )


def test_authorize_page_is_served_and_private(c):
    r = c.get("/oauth/authorize?client_id=x")
    assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"]
    assert "Disallow: /oauth/" in c.get("/robots.txt").text


def _magic_token():
    from jobmcp import mailer

    m = mailer.outbox[-1]
    assert m.purpose == "magic"
    link = next(w for w in m.body.split() if "magic=" in w)
    return m, parse_qs(urlsplit(link).query)["magic"][0], link


def test_an_agent_can_bring_a_new_user_in_with_just_an_email(c):
    from jobmcp import db, mailer
    from jobmcp.orm import User

    cid = register(c).json()["client_id"]
    body = {**authorize_params(cid), "email": "New@X.io"}
    r = c.post("/api/oauth/email-link", json=body)
    assert r.status_code == 202
    m, raw, link = _magic_token()
    assert m.to == "new@x.io"
    assert "Test Agent" in m.body and "127.0.0.1:33418" in m.body
    # the link returns to this same request, so the user lands on Approve
    assert urlsplit(link).path == "/oauth/authorize"
    assert parse_qs(urlsplit(link).query)["client_id"] == [cid]
    # the account exists but is not confirmed until the link is opened
    with db.session() as s:
        u = s.query(User).filter_by(email="new@x.io").one()
        assert u.email_verified_at is None
    r = c.post("/api/auth/magic", json={"token": raw})
    assert r.status_code == 200 and r.json()["user"]["email"] == "new@x.io"
    h = {"Authorization": "Bearer " + r.json()["token"]}
    with db.session() as s:
        assert (
            s.query(User).filter_by(email="new@x.io").one().email_verified_at
        )
    # a link works once
    assert c.post("/api/auth/magic", json={"token": raw}).status_code == 400
    # and the session can approve the request
    d = c.post(
        "/api/oauth/decision",
        json={**authorize_params(cid), "approve": True},
        headers=h,
    )
    assert d.status_code == 200 and "code=" in d.json()["redirect_to"]
    assert len(mailer.outbox) == 1


def test_email_link_for_an_existing_user_and_no_account_leak(c):
    from jobmcp import mailer

    mk_user("old@x.io")
    cid = register(c).json()["client_id"]
    a = c.post(
        "/api/oauth/email-link",
        json={**authorize_params(cid), "email": "old@x.io"},
    )
    assert a.status_code == 202 and len(mailer.outbox) == 1
    _, raw, _ = _magic_token()
    r = c.post("/api/auth/magic", json={"token": raw})
    assert r.json()["user"]["email"] == "old@x.io"
    b = c.post(
        "/api/oauth/email-link",
        json={**authorize_params(cid), "email": "other@x.io"},
    )
    assert b.json() == a.json()  # same answer whoever the address is


def test_email_link_refuses_a_bad_request_and_closed_registration(
    c, monkeypatch
):
    from jobmcp import mailer

    cid = register(c).json()["client_id"]
    bad = {**authorize_params(cid, redirect_uri="https://evil.example/x")}
    r = c.post("/api/oauth/email-link", json={**bad, "email": "v@x.io"})
    assert r.status_code == 400 and not mailer.outbox
    monkeypatch.setattr(config, "ALLOW_REGISTRATION", False)
    r = c.post(
        "/api/oauth/email-link",
        json={**authorize_params(cid), "email": "nobody@x.io"},
    )
    assert r.status_code == 202 and not mailer.outbox
    assert c.post("/api/auth/magic", json={"token": "nope"}).status_code == 400
