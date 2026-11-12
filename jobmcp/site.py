"""The whole product in one ASGI app: web pages, REST API (/api) and the MCP
endpoint (/mcp)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import logging
import mimetypes
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import config, db, logs, mcp_server, metrics
from .api import app as api_app

# Minimal OS images (python:slim) have no MIME table entry for web fonts;
# without this they are served as octet-stream.
mimetypes.add_type("font/woff2", ".woff2")

WEB = Path(__file__).parent / "web"
PAGES = {
    "/": "index.html",
    "/jobs": "jobs.html",
    "/login": "login.html",
    "/register": "register.html",
    "/portal": "portal.html",
    "/privacy": "privacy.html",
    "/verify": "verify.html",
    "/forgot": "forgot.html",
    "/reset": "reset.html",
}

# Everything is same-origin and script/style are external files, so no
# 'unsafe-inline' anywhere.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' "
    "data:; font-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action "
    "'self'; object-src 'none'"
)

log = logging.getLogger("jobmcp.web")
mcp_asgi, mcp_inner = mcp_server.build_http("/")
QUIET = (
    "/healthz",
    "/metrics",
    "/static/",
)  # polled constantly: logged at DEBUG only


class RequestLog:
    """One line per request: method, path (never the query string, which can
    carry tokens), status, time, client.
    Each request gets an id, returned as X-Request-ID and attached to every log
    line written while handling it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex[:12]
        tok = logs.request_id.set(rid)
        started, status = time.monotonic(), 500

        async def send_wrapped(msg):
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
                msg["headers"] = [
                    *msg.get("headers", []),
                    (b"x-request-id", rid.encode()),
                ]
            await send(msg)

        try:
            await self.app(scope, receive, send_wrapped)
        except Exception:
            log.exception("%s %s crashed", scope["method"], scope["path"])
            raise
        finally:
            path = scope["path"]
            level = (
                logging.DEBUG
                if path.startswith(QUIET)
                else (logging.WARNING if status >= 500 else logging.INFO)
            )
            client = scope.get("client")
            log.log(
                level,
                "%s %s %d %dms",
                scope["method"],
                path,
                status,
                (time.monotonic() - started) * 1000,
                extra={"ip": client[0] if client else "-"},
            )
            logs.request_id.reset(tok)


def startup_warnings() -> list[str]:
    """Settings that work in dev but quietly break a real deployment."""
    out = []
    if not config.DEV_MODE:
        if config.REQUIRE_EMAIL_VERIFICATION and config.MAIL_BACKEND != "smtp":
            out.append(
                f"JOBMCP_MAIL_BACKEND={config.MAIL_BACKEND!r} but email "
                "verification is required: users will never receive "
                "their confirmation or password-reset links. Configure SMTP "
                "(JOBMCP_MAIL_BACKEND=smtp), or set "
                "JOBMCP_REQUIRE_EMAIL_VERIFICATION=0."
            )
        if (
            "localhost" in config.PUBLIC_BASE_URL
            or "127.0.0.1" in config.PUBLIC_BASE_URL
        ):
            out.append(
                f"JOBMCP_PUBLIC_BASE_URL is {config.PUBLIC_BASE_URL!r}: "
                "links in emails and the agent setup "
                "commands will point at localhost. Set it to your public "
                "https:// address."
            )
    return out


@asynccontextmanager
async def lifespan(_app):
    # started as plain `uvicorn jobmcp.site:app` rather than through the CLI
    if not logs.configured():
        logs.setup()
    log.info(
        "jobmcp web starting: log_level=%s mail=%s dev=%s public_url=%s",
        logging.getLevelName(logging.getLogger().level),
        config.MAIL_BACKEND,
        config.DEV_MODE,
        config.PUBLIC_BASE_URL,
    )
    # fail at startup, with a clear message, if the database URL isn't a MySQL
    # one
    db.mysql_url()
    for w in startup_warnings():
        logging.getLogger("jobmcp.config").warning(w)
    async with mcp_inner.router.lifespan_context(mcp_inner):
        yield
    log.info("jobmcp web stopped")


app = FastAPI(
    title="jobmcp",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/api", api_app)
app.mount("/mcp", mcp_asgi)
app.mount("/static", StaticFiles(directory=WEB), name="static")


def _page(name: str):
    def handler():
        return FileResponse(WEB / name, media_type="text/html")

    return handler


for path, name in PAGES.items():
    app.add_api_route(
        path, _page(name), methods=["GET"], include_in_schema=False
    )


@app.get("/healthz", include_in_schema=False)
def healthz():
    return JSONResponse({"ok": True})


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint(request: Request):
    if not metrics.authorized(
        request.client.host if request.client else None, request.headers
    ):
        return PlainTextResponse("forbidden\n", status_code=403)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _handler_label(request: Request) -> str:
    """The route *template* (/api/jobs/{job_id}), never the raw path, so label
    cardinality stays bounded."""
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return request.scope.get("root_path", "") + route.path
    p = request.url.path
    if p.startswith("/static/"):
        return "/static"
    if p.startswith("/mcp"):
        return "/mcp"
    return "unmatched"


@app.middleware("http")
async def security_headers(request, call_next):
    start = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:
        metrics.HTTP_REQUESTS.labels(
            request.method, _handler_label(request), "500"
        ).inc()
        raise
    handler = _handler_label(request)
    metrics.HTTP_REQUESTS.labels(
        request.method, handler, str(resp.status_code)
    ).inc()
    metrics.HTTP_LATENCY.labels(request.method, handler).observe(
        time.perf_counter() - start
    )
    p = request.url.path
    if not p.startswith(("/api", "/mcp")):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if p.startswith("/static/") and not p.endswith((".woff2",)):
            resp.headers.setdefault(
                "Cache-Control", "no-cache"
            )  # always revalidate; assets are tiny
    return resp


class PublicGZip(GZipMiddleware):
    """Compress what is public (the pages, scripts, styles and the public jobs
    API). Never the signed-in API or the agent endpoint: compressing a
    response that holds a secret next to attacker-influenced text is the
    BREACH attack, and those responses are small anyway."""

    PREFIXES = ("/static/", "/api/public/")

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (
            path in ("/", "/jobs", "/privacy", "/portal")
            or path.startswith(self.PREFIXES)
        ):
            return await super().__call__(scope, receive, send)
        return await self.app(scope, receive, send)


app.add_middleware(PublicGZip, minimum_size=1024)
app.add_middleware(
    RequestLog
)  # added last, so it is outermost and sees every request
