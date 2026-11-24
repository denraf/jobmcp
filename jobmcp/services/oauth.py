"""Agents sign themselves in: OAuth 2.1 authorization-code flow with PKCE and
dynamic client registration, which is what MCP clients (Claude, Codex, Gemini
CLI, ...) speak. The user approves once on the website, and the agent holds a
short-lived access token plus a refresh token. Nothing is copied by hand.

The access token is an ordinary `mcp` ApiToken, so every MCP tool, the rate
limit and token revocation work as for a pasted token; it just expires after
an hour and is replaced through the refresh token. Pasted long-lived tokens
keep working unchanged.

Public clients only (no client secret): the PKCE verifier proves the agent that
asked is the one that receives the code. S256 is required.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .. import config
from ..orm import (
    ApiToken,
    OAuthClient,
    OAuthCode,
    OAuthGrant,
    OAuthRefresh,
    User,
    utcnow,
)
from ..security import hash_token
from . import ServiceError, auth

SCOPE = "jobs"
CODE_TTL = timedelta(seconds=60)
MAX_REDIRECTS = 5
LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}
_UNSAFE_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob"}
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # base64url of a SHA-256
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


class OAuthError(Exception):
    """An OAuth error response: `error` is the RFC 6749 code."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error, self.description, self.status = error, description, status


def issuer() -> str:
    return config.PUBLIC_BASE_URL


def resource() -> str:
    return config.PUBLIC_MCP_URL.rstrip("/")


# ── clients ─────────────────────────────────────────────────────────────
def _valid_redirect(uri: str) -> bool:
    if not isinstance(uri, str) or not 0 < len(uri) <= 500 or "#" in uri:
        return False
    u = urlsplit(uri)
    if u.username or u.password:
        return False
    if u.scheme == "https":
        return bool(u.hostname)
    if u.scheme == "http":  # only the machine the agent runs on
        return (u.hostname or "") in {h.strip("[]") for h in LOOPBACK}
    # a native app's own scheme, e.g. cursor://anysphere.cursor/oauth
    return bool(
        re.fullmatch(r"[a-z][a-z0-9+.-]*", u.scheme)
        and u.scheme not in _UNSAFE_SCHEMES
    )


def register_client(s: Session, meta: dict) -> dict:
    uris = meta.get("redirect_uris")
    if (
        not isinstance(uris, list)
        or not 1 <= len(uris) <= MAX_REDIRECTS
        or not all(_valid_redirect(u) for u in uris)
    ):
        raise OAuthError(
            "invalid_redirect_uri",
            "redirect_uris: 1 to 5 https, loopback http or app-scheme URIs",
        )
    if meta.get("token_endpoint_auth_method", "none") != "none":
        raise OAuthError(
            "invalid_client_metadata",
            "only public clients are supported "
            "(token_endpoint_auth_method 'none')",
        )
    name = re.sub(r"\s+", " ", str(meta.get("client_name") or "")).strip()
    c = OAuthClient(
        id="jmc_" + secrets.token_urlsafe(18),
        name=name[:100] or "An agent",
        redirect_uris=list(dict.fromkeys(uris)),
    )
    s.add(c)
    s.flush()
    return {
        "client_id": c.id,
        "client_name": c.name,
        "redirect_uris": c.redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_id_issued_at": int(c.created_at.timestamp())
        if c.created_at
        else 0,
    }


def _redirect_matches(client: OAuthClient, uri: str) -> bool:
    if uri in client.redirect_uris:
        return True
    # RFC 8252: a loopback redirect may use any port
    u = urlsplit(uri)
    if u.scheme == "http" and (u.hostname or "") in LOOPBACK:
        return any(
            (r := urlsplit(x)).scheme == "http"
            and r.hostname == u.hostname
            and r.path == u.path
            and r.query == u.query
            for x in client.redirect_uris
        )
    return False


# ── authorization ───────────────────────────────────────────────────────
def check_authorize(s: Session, p: dict) -> OAuthClient:
    """Validate an authorization request. Problems with the client or the
    redirect are shown to the user, never redirected to."""
    c = s.get(OAuthClient, p.get("client_id") or "")
    if not c:
        raise OAuthError("invalid_client", "this app is not registered")
    uri = p.get("redirect_uri") or ""
    if not _redirect_matches(c, uri):
        raise OAuthError(
            "invalid_request", "redirect_uri does not match the registration"
        )
    if p.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "use response_type=code")
    if p.get("code_challenge_method") != "S256" or not _CHALLENGE.match(
        p.get("code_challenge") or ""
    ):
        raise OAuthError(
            "invalid_request", "PKCE with code_challenge_method=S256 required"
        )
    res = p.get("resource")
    if res and res.rstrip("/") != resource():
        raise OAuthError("invalid_target", "unknown resource")
    return c


def _back(uri: str, **params) -> str:
    u = urlsplit(uri)
    q = parse_qsl(u.query, keep_blank_values=True) + [
        (k, v) for k, v in params.items() if v
    ]
    return urlunsplit(u._replace(query=urlencode(q)))


def approve(s: Session, user: User, p: dict, allow: bool) -> str:
    """The user's decision. Returns where to send the browser: the client's
    redirect_uri with a one-time code (or the refusal) and its `state`."""
    c = check_authorize(s, p)
    uri, state = p["redirect_uri"], p.get("state") or ""
    if not allow:
        return _back(uri, error="access_denied", state=state, iss=issuer())
    g = OAuthGrant(user_id=user.id, client_id=c.id)
    s.add(g)
    s.flush()
    code = secrets.token_urlsafe(32)
    s.add(
        OAuthCode(
            code_hash=hash_token(code),
            grant_id=g.id,
            redirect_uri=uri,
            code_challenge=p["code_challenge"],
            expires_at=utcnow() + CODE_TTL,
        )
    )
    return _back(uri, code=code, state=state, iss=issuer())


# ── tokens ──────────────────────────────────────────────────────────────
def _mint(s: Session, grant: OAuthGrant, client: OAuthClient) -> dict:
    user = s.get(User, grant.user_id)
    raw, t = auth.issue_token(
        s, user, "mcp", name=f"{client.name} (signed in)"
    )
    t.grant_id = grant.id
    t.expires_at = utcnow() + timedelta(minutes=config.OAUTH_ACCESS_TTL_MIN)
    refresh = secrets.token_urlsafe(40)
    s.add(
        OAuthRefresh(
            token_hash=hash_token(refresh),
            grant_id=grant.id,
            expires_at=utcnow()
            + timedelta(days=config.OAUTH_REFRESH_TTL_DAYS),
        )
    )
    grant.last_used_at = utcnow()
    return {
        "access_token": raw,
        "token_type": "Bearer",
        "expires_in": config.OAUTH_ACCESS_TTL_MIN * 60,
        "refresh_token": refresh,
        "scope": SCOPE,
    }


def revoke_grant(s: Session, grant_id: int) -> None:
    now = utcnow()
    s.execute(
        update(OAuthGrant)
        .where(OAuthGrant.id == grant_id, OAuthGrant.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    s.execute(
        update(ApiToken)
        .where(ApiToken.grant_id == grant_id, ApiToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    s.execute(delete(OAuthRefresh).where(OAuthRefresh.grant_id == grant_id))


def _live_grant(s: Session, grant_id: int, client_id: str) -> OAuthGrant:
    g = s.get(OAuthGrant, grant_id)
    u = s.get(User, g.user_id) if g else None
    if (
        not g
        or g.revoked_at
        or g.client_id != client_id
        or not u
        or not u.is_active
    ):
        raise OAuthError("invalid_grant", "this sign-in was revoked")
    return g


def exchange_code(
    s: Session,
    client_id: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    res: str | None = None,
) -> dict:
    client = s.get(OAuthClient, client_id or "")
    if not client:
        raise OAuthError("invalid_client", "unknown client", 401)
    if res and res.rstrip("/") != resource():
        raise OAuthError("invalid_target", "unknown resource")
    row = s.get(OAuthCode, hash_token(code or ""))
    bad = OAuthError("invalid_grant", "the code is invalid or expired")
    if not row:
        raise bad
    # single use: claim it atomically, so two requests cannot both win
    claimed = s.execute(
        update(OAuthCode)
        .where(
            OAuthCode.code_hash == row.code_hash, OAuthCode.used_at.is_(None)
        )
        .values(used_at=utcnow())
    ).rowcount
    if not claimed:
        # a replayed code: whoever got tokens from it must lose them
        revoke_grant(s, row.grant_id)
        s.commit()
        raise bad
    grant = s.get(OAuthGrant, row.grant_id)
    if (
        row.expires_at < utcnow()
        or row.redirect_uri != redirect_uri
        or grant.client_id != client.id
    ):
        raise bad
    if not _VERIFIER.match(verifier or "") or not hmac.compare_digest(
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode(),
        row.code_challenge,
    ):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    _live_grant(s, grant.id, client.id)
    _tidy(s)
    return _mint(s, grant, client)


def refresh(s: Session, client_id: str, token: str) -> dict:
    client = s.get(OAuthClient, client_id or "")
    if not client:
        raise OAuthError("invalid_client", "unknown client", 401)
    row = s.get(OAuthRefresh, hash_token(token or ""))
    if not row:
        raise OAuthError("invalid_grant", "unknown refresh token")
    grant = _live_grant(s, row.grant_id, client.id)
    if row.used_at or row.expires_at < utcnow():
        if row.used_at:  # replay of a rotated token: assume it leaked
            revoke_grant(s, grant.id)
            s.commit()
        raise OAuthError(
            "invalid_grant", "the refresh token is no longer valid"
        )
    # claim it atomically, as for codes
    if not s.execute(
        update(OAuthRefresh)
        .where(
            OAuthRefresh.token_hash == row.token_hash,
            OAuthRefresh.used_at.is_(None),
        )
        .values(used_at=utcnow())
    ).rowcount:
        raise OAuthError("invalid_grant", "the refresh token was just used")
    return _mint(s, grant, client)


def _tidy(s: Session) -> None:
    """Drop what can never be used again."""
    now = utcnow()
    s.execute(delete(OAuthCode).where(OAuthCode.expires_at < now))
    s.execute(delete(OAuthRefresh).where(OAuthRefresh.expires_at < now))
    s.execute(
        delete(ApiToken).where(
            ApiToken.grant_id.is_not(None),
            ApiToken.expires_at < now - timedelta(days=1),
        )
    )


# ── what the user sees ──────────────────────────────────────────────────
def list_grants(s: Session, user: User) -> list[dict]:
    rows = s.execute(
        select(OAuthGrant, OAuthClient.name)
        .join(OAuthClient, OAuthClient.id == OAuthGrant.client_id)
        .where(OAuthGrant.user_id == user.id, OAuthGrant.revoked_at.is_(None))
        .order_by(OAuthGrant.id.desc())
    ).all()
    return [
        {
            "id": g.id,
            "name": name,
            "created_at": g.created_at,
            "last_used_at": g.last_used_at,
        }
        for g, name in rows
    ]


def revoke_for_user(s: Session, user: User, grant_id: int) -> None:
    g = s.get(OAuthGrant, grant_id)
    if not g or g.user_id != user.id:
        raise ServiceError("connected app not found", 404)
    revoke_grant(s, grant_id)
