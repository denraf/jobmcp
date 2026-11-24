"""The OAuth endpoints an MCP client discovers on its own: the metadata
documents, dynamic client registration and the token endpoint. The page where
the user approves is /oauth/authorize (web/authorize.html), which asks the REST
API (/api/oauth/...) to record the decision."""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from . import db, metrics
from .ratelimit import auth_limit
from .services import oauth

log = logging.getLogger("jobmcp.oauth")
router = APIRouter()

# Browser-based MCP clients call these from another origin. None of them reads
# a cookie or a token, so any origin may.
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, "
    "MCP-Protocol-Version",
    "Access-Control-Max-Age": "86400",
}
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", **CORS}


def _json(body: dict, status: int = 200, cache: bool = False) -> JSONResponse:
    return JSONResponse(
        body,
        status_code=status,
        headers={**CORS, "Cache-Control": "public, max-age=3600"}
        if cache
        else NO_STORE,
    )


def _error(e: oauth.OAuthError) -> JSONResponse:
    body = {"error": e.error}
    if e.description:
        body["error_description"] = e.description
    headers = dict(NO_STORE)
    if e.status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="jobmcp"'
    return JSONResponse(body, status_code=e.status, headers=headers)


@router.options("/oauth/{rest:path}", include_in_schema=False)
@router.options("/.well-known/{rest:path}", include_in_schema=False)
def preflight(rest: str = ""):
    return Response(status_code=204, headers=CORS)


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get(
    "/.well-known/oauth-protected-resource/{rest:path}",
    include_in_schema=False,
)
def protected_resource(rest: str = ""):
    """RFC 9728: where the MCP endpoint says to sign in."""
    return _json(
        {
            "resource": oauth.resource(),
            "authorization_servers": [oauth.issuer()],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [oauth.SCOPE],
            "resource_name": "jobmcp",
        },
        cache=True,
    )


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get(
    "/.well-known/oauth-authorization-server/{rest:path}",
    include_in_schema=False,
)
def authorization_server(rest: str = ""):
    """RFC 8414."""
    b = oauth.issuer()
    return _json(
        {
            "issuer": b,
            "authorization_endpoint": f"{b}/oauth/authorize",
            "token_endpoint": f"{b}/oauth/token",
            "registration_endpoint": f"{b}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [oauth.SCOPE],
            "authorization_response_iss_parameter_supported": True,
        },
        cache=True,
    )


@router.post(
    "/oauth/register",
    status_code=201,
    include_in_schema=False,
    dependencies=[Depends(auth_limit)],
)
async def register(request: Request):
    """RFC 7591: an agent app introduces itself and gets a client_id."""
    try:
        meta = await request.json()
        if not isinstance(meta, dict):
            raise ValueError
    except Exception:
        return _error(
            oauth.OAuthError("invalid_client_metadata", "send a JSON object")
        )
    try:
        with db.session() as s:
            out = oauth.register_client(s, meta)
    except oauth.OAuthError as e:
        return _error(e)
    metrics.AUTH_EVENTS.labels("oauth_client_registered").inc()
    log.info("oauth client registered", extra={"client": out["client_id"]})
    return _json(out, 201)


@router.post(
    "/oauth/token",
    include_in_schema=False,
    dependencies=[Depends(auth_limit)],
)
async def token(request: Request):
    form = dict(
        parse_qsl((await request.body()).decode("utf-8", "replace")[:8192])
    )
    try:
        with db.session() as s:
            kind = form.get("grant_type")
            if kind == "authorization_code":
                out = oauth.exchange_code(
                    s,
                    form.get("client_id", ""),
                    form.get("code", ""),
                    form.get("redirect_uri", ""),
                    form.get("code_verifier", ""),
                    form.get("resource"),
                )
                metrics.AUTH_EVENTS.labels("oauth_signed_in").inc()
            elif kind == "refresh_token":
                out = oauth.refresh(
                    s,
                    form.get("client_id", ""),
                    form.get("refresh_token", ""),
                )
                metrics.AUTH_EVENTS.labels("oauth_refreshed").inc()
            else:
                raise oauth.OAuthError(
                    "unsupported_grant_type",
                    "use authorization_code or refresh_token",
                )
    except oauth.OAuthError as e:
        log.info("oauth token refused: %s %s", e.error, e.description)
        return _error(e)
    return _json(out)
