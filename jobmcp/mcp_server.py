"""Per-user MCP server.

Every tool acts as the user who owns the token and nobody else — there is no
tool that takes a
user id. HTTP: `Authorization: Bearer <mcp token>` on each request. stdio:
JOBMCP_TOKEN in the env.

The server does not apply to anything and holds no résumé or contact details.
It hands the connected
agent (Claude, Codex, Gemini, ...) the matching jobs and the apply URL; the
agent applies using what the
user has given *it*, then reports back only a status, which is all the ledger
records.
"""

from __future__ import annotations

import contextvars
import logging
import os
import time

from functools import wraps

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import config, db, metrics
from .models import strip_data_uris
from .orm import User
from .services import (
    ServiceError,
    applications,
    auth,
    matching,
    prefs,
    saved,
    search,
)

log = logging.getLogger("jobmcp.mcp")
_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "jobmcp_user_id", default=None
)


# What an agent must do on the application form itself. jobmcp cannot see the
# form, so this is the one place the rules are written down: the server
# instructions, the apply prompt and every start_application reply carry them.
APPLY_RULES = (
    "ANSWERING THE FORM. Read the whole application first, every page and "
    "step, and list every field and question. Fill in only what the user has "
    "already given you (their résumé, name, contact details, links, work "
    "history). For EVERYTHING ELSE, ask the user and wait for their answer: "
    "never guess, infer, assume a default, or copy an answer from another "
    "application unless the user said to reuse it. That includes: work "
    "authorization and visa sponsorship (now or in the future), relocation, "
    "salary expectations, notice period and start date, security clearance or "
    "background-check consent, references, referral names, how they heard "
    "about the job, cover letter or 'why us' answers, and EVERY custom "
    "question the company added (free text, multiple choice, yes/no, "
    "assessments, extra uploads). Voluntary self-identification questions "
    "(disability, veteran status, race or ethnicity, gender, sexual "
    "orientation, age) are personal: ask the user each one, offer 'prefer "
    "not to answer / decline to self-identify' whenever the form allows it, "
    "and never infer an answer from the user's name, photo or résumé. "
    "Declarations, certifications, consents and e-signatures are the user's "
    "to approve: ask first. Ask efficiently: at the start of a run, ask once "
    "about the usual ones (visa sponsorship, work authorization, the "
    "self-identification questions or 'decline to answer', salary, notice "
    "period, relocation) and offer to reuse those answers for the rest of "
    "the run; ask again only for questions that are new. If the user can't "
    "be asked or doesn't answer, do not submit: record 'skipped' with a note "
    "that names the topic (for example 'needs a visa sponsorship answer'). "
    "Never put the user's answers, or any personal detail, in jobmcp: not "
    "in notes, not anywhere. If the application form is on a site other than "
    "the company's own career page or its ATS, check with the user before "
    "entering any personal details."
)

# the short version that travels with every claimed job
APPLY_REMINDER = (
    "Before you submit: read the whole form. Ask the user (do not guess) for "
    "anything they have not already given you: visa sponsorship and work "
    "authorization, disability, veteran status, race or ethnicity, gender and "
    "other self-identification questions (offer 'decline to answer'), "
    "salary, notice period, relocation, consents and e-signatures, and every "
    "custom question. If you cannot get an answer, record 'skipped' naming "
    "the topic. Keep the user's answers out of jobmcp, including notes."
)

mcp = MCPServer(
    "jobmcp",
    instructions=(
        "Job search and application tracker for one user. jobmcp stores job "
        "listings, the user's search "
        "preferences, saved jobs and a ledger of application statuses. It "
        "does NOT store the user's résumé, "
        "contact details or answers, and you must never send those to it: use "
        "what the user has given you "
        "directly, and ask the user if something is missing. Workflow: "
        "get_my_preferences, then repeatedly "
        "list_matching_jobs (or search_jobs for a specific title/keyword) -> "
        "start_application -> open the job's "
        "apply_url and complete the form yourself -> "
        "record_application_result. Never apply to a job you did "
        "not start_application for. " + APPLY_RULES
    ),
)


def tool():
    """Register a tool, surfacing ServiceError messages ("daily cap reached",
    "already applied") to the
    agent. Any other exception is a bug and stays a generic error so internals
    don't leak."""

    def deco(fn):
        @wraps(fn)
        def inner(*a, **kw):
            started, uid = time.monotonic(), _user_id.get()
            try:
                result = fn(*a, **kw)
                metrics.MCP_CALLS.labels(fn.__name__, "ok").inc()
                log.info(
                    "tool %s ok",
                    fn.__name__,
                    extra={
                        "user_id": uid,
                        "ms": int((time.monotonic() - started) * 1000),
                    },
                )
                return result
            except ServiceError as e:
                metrics.MCP_CALLS.labels(
                    fn.__name__,
                    "refused",
                    # an expected refusal (cap reached, already applied...)
                ).inc()
                log.info(
                    "tool %s refused: %s",
                    fn.__name__,
                    e,
                    extra={"user_id": uid},
                )
                raise ToolError(str(e)) from e
            except Exception:
                metrics.MCP_CALLS.labels(fn.__name__, "error").inc()
                log.exception(
                    "tool %s failed", fn.__name__, extra={"user_id": uid}
                )
                raise

        mcp.tool()(inner)
        return fn

    return deco


def _uid() -> int:
    uid = _user_id.get()
    if uid is None:
        raise ServiceError("unauthenticated", 401)
    return uid


def _job(j, full=False) -> dict:
    d = {
        "job_id": j.id,
        "title": j.title,
        "company": j.company,
        "location": j.location,
        "vendor": j.vendor,
        "workplace": j.workplace,
        "seniority": j.seniority,
        "country": j.country,
        "staffing_agency": bool(j.board.is_agency),
        "apply_url": j.url,
        "posted_at": j.posted_at.isoformat() if j.posted_at else None,
        "listed_at": j.sort_at.isoformat() if j.sort_at else None,
    }
    if full:
        # Pictures embedded in a posting are hundreds of kilobytes of base64:
        # the agent gets the text, with '[image]' where each picture was.
        d["description"] = strip_data_uris(j.description)
    return d


def _app(a) -> dict:
    return {
        "job_id": a.job_id,
        "status": a.status,
        "notes": a.notes,
        "applied_at": a.applied_at.isoformat() if a.applied_at else None,
    }


@tool()
def get_my_preferences() -> dict:
    """The user's job preferences (what to look for) and how many applications
    they have started today.
    Contains no personal details: jobmcp does not hold the user's résumé or
    contact information."""
    with db.session() as s:
        u = s.get(User, _uid())
        return {
            "preferences": prefs.prefs_dict(prefs.get_preferences(s, u)),
            "applications_today": applications.applications_today(s, u.id),
        }


@tool()
def list_matching_jobs(limit: int = 10, cursor: int | None = None) -> dict:
    """Open jobs matching the user's preferences that they have not applied to
    or skipped.
    Pass `next_cursor` back as `cursor` for the next page (null = no more)."""
    with db.session() as s:
        u = s.get(User, _uid())
        jobs, nxt = matching.find_matches(
            s,
            u.id,
            prefs.get_preferences(s, u),
            min(max(limit, 1), 50),
            cursor,
        )
        return {"jobs": [_job(j) for j in jobs], "next_cursor": nxt}


@tool()
def search_jobs(
    title: str | None = None,
    keywords: str | None = None,
    match: str = "all",
    location: str | None = None,
    remote_only: bool = False,
    vendor: str | None = None,
    company: str | None = None,
    max_age_days: int | None = None,
    include_applied: bool = False,
    limit: int = 20,
    offset: int = 0,
    workplace: str | None = None,
    seniority: list[str] | None = None,
    country: list[str] | None = None,
    exclude_agencies: bool = False,
    sort: str = "relevance",
) -> dict:
    """Search ALL open jobs (not just the user's preference matches) by job
    title and/or any keyword
    in the title or description. Give `title` and/or `keywords`.
    Syntax: space-separated terms, all required; "quoted phrase"; engineer* =
    prefix; -term = exclude.
    Whole-word, case-insensitive ('go' will not match 'Google').
    `match`: 'all' (default) or 'any' keyword term. Optional filters: location,
    remote_only, vendor, company,
    max_age_days; workplace ('remote' or 'hybrid'); seniority (any of intern,
    entry, mid, senior, lead, executive; 'mid' = no seniority word in the
    title); country (ISO codes like US, GB, DE); exclude_agencies=true hides
    staffing/recruiting agencies. Applied/skipped jobs are hidden unless
    include_applied=true.
    sort: 'relevance' (default) ranks title hits first, newest within each;
    'newest' is strictly by publish date. Results include listed_at (the
    publish date, else when we first saw the job) and a description snippet;
    page with next_offset. Use
    start_application on a job_id to apply."""
    with db.session() as s:
        r = search.search_jobs(
            s,
            _uid(),
            title,
            keywords,
            match,
            location,
            remote_only,
            vendor,
            company,
            max_age_days,
            include_applied,
            limit,
            offset,
            workplace=workplace,
            seniority=seniority,
            country=country,
            exclude_agencies=exclude_agencies,
            sort=sort,
        )
        return {
            **{k: v for k, v in r.items() if k != "hits"},
            "hits": [
                {
                    **_job(h.job),
                    "title_match": h.in_title,
                    "snippet": h.snippet,
                    "my_status": h.status,
                }
                for h in r["hits"]
            ],
        }


@tool()
def get_job(job_id: int) -> dict:
    """Full job description and apply URL."""
    from .orm import Job

    with db.session() as s:
        j = s.get(Job, job_id)
        if not j:
            raise ServiceError("job not found", 404)
        return _job(j, full=True)


@tool()
def start_application(job_id: int) -> dict:
    """Claim a job before applying (prevents duplicate applications and
    enforces the user's daily cap).
    Returns the job and its apply_url; you then apply on the company's site
    with the user's own details, and you ASK THE USER for anything on the form
    they have not already given you (visa sponsorship, disability, race,
    gender, other self-identification questions, custom questions): see
    `before_you_submit` in the reply.
    Errors if the job is closed, already applied/skipped, or the cap is
    reached."""
    from .orm import Job

    with db.session() as s:
        u = s.get(User, _uid())
        app, resumed = applications.start(s, u, job_id)
        return {
            "claimed": True,
            "resumed": resumed,
            "job": _job(s.get(Job, job_id), full=True),
            "before_you_submit": APPLY_REMINDER,
        }


@tool()
def record_application_result(
    job_id: int, status: str, notes: str = ""
) -> dict:
    """Report the outcome after attempting an application.
    status: applied | failed | skipped (also interview | offer | rejected for
    follow-ups).
    Use 'applied' only after the site confirmed the submission. If you could
    not finish because the user did not answer a question, use 'skipped' and
    name the topic in `notes` (for example 'needs a visa sponsorship
    answer'). `notes` is kept: never put the user's answers or personal
    details in it."""
    with db.session() as s:
        u = s.get(User, _uid())
        return _app(
            applications.record(s, u, job_id, status, notes, method="mcp")
        )


@tool()
def list_my_applications(
    status: str | None = None, limit: int = 50
) -> list[dict]:
    """The user's application ledger, newest first."""
    with db.session() as s:
        return [
            {**_job(a.job), **_app(a)}
            for a in applications.list_for_user(s, _uid(), status, limit)
        ]


@tool()
def save_job(job_id: int) -> dict:
    """Bookmark a job for the user to look at later (shown in their portal).
    Does not apply."""
    with db.session() as s:
        saved.save(s, _uid(), job_id)
        return {"saved": True, "job_id": job_id}


@tool()
def unsave_job(job_id: int) -> dict:
    """Remove a bookmark."""
    with db.session() as s:
        saved.unsave(s, _uid(), job_id)
        return {"saved": False, "job_id": job_id}


@tool()
def list_saved_jobs(limit: int = 50) -> list[dict]:
    """The user's saved jobs, newest first. `open` is false if the posting has
    since closed."""
    with db.session() as s:
        return [
            {**_job(j), "open": o}
            for j, o in saved.list_saved(s, _uid(), limit)
        ]


@tool()
def update_my_preferences(preferences: dict) -> dict:
    """Update job preferences. Keys: title_include, title_exclude, locations,
    vendors, companies_include,
    companies_exclude, description_exclude (lists of strings; trailing * =
    prefix match), remote_only,
    max_age_days, max_applications_per_day."""
    with db.session() as s:
        u = s.get(User, _uid())
        return prefs.prefs_dict(prefs.update_preferences(s, u, preferences))


@mcp.prompt()
def apply_to_matching_jobs(max_jobs: int = 5) -> str:
    """Work through the user's matching jobs."""
    return (
        f"Apply to up to {max_jobs} matching jobs for me. Call "
        "get_my_preferences, then loop: list_matching_jobs; "
        "for each job call start_application, open apply_url in your browser, "
        "and fill in the form using my résumé and the details I have given "
        "you (jobmcp does not have them). "
        + APPLY_RULES
        + " Call record_application_result with 'applied' only once the site "
        "confirms submission. If the form needs an account or a captcha you "
        "can't pass, record 'failed' with a short note and move on. Stop at "
        "the daily cap and summarise what you did, and which questions you "
        "asked me."
    )


# ── transports ──────────────────────────────────────────────────────────
class BearerAuth:
    """ASGI middleware: resolves the MCP token to a user for the duration of
    the request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        h = headers.get("authorization", "")
        uid = None
        if h.lower().startswith("bearer "):
            with db.session() as s:
                u = auth.resolve_token(s, h[7:].strip(), "mcp")
                uid = u.id if u else None
        if uid is None:
            log.warning(
                "MCP request refused: missing or invalid token",
                extra={"path": scope.get("path", "")},
            )
            body = b'{"error":"invalid or missing MCP token"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        log.debug(
            "MCP request",
            extra={"user_id": uid, "path": scope.get("path", "")},
        )
        tok = _user_id.set(uid)
        try:
            await self.app(scope, receive, send)
        finally:
            _user_id.reset(tok)


def transport_security() -> TransportSecuritySettings:
    """Which Host and Origin headers the endpoint accepts. The library's
    default allows localhost only, which refuses every request that arrives
    through the reverse proxy under the real domain (421 Invalid Host
    header)."""
    from urllib.parse import urlsplit

    hosts = {"127.0.0.1", "localhost", "[::1]", *config.ALLOWED_HOSTS}
    for url in (config.PUBLIC_BASE_URL, config.PUBLIC_MCP_URL):
        if urlsplit(url).netloc:
            hosts.add(urlsplit(url).netloc)
    allowed_hosts = sorted(hosts | {f"{h}:*" for h in hosts})
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        # agents send no Origin; a browser page on another site does
        allowed_origins=[
            f"{scheme}://{h}"
            for h in allowed_hosts
            for scheme in ("http", "https")
        ],
    )


def build_http(path: str = "/mcp"):
    """-> (asgi_app, inner_starlette). Stateless: every request carries its own
    token, so no server-side
    session can outlive a revoked token. When mounting under another app, the
    caller must run
    `inner.router.lifespan_context(inner)` (the session manager lives
    there)."""
    inner = mcp.streamable_http_app(
        stateless_http=True,
        streamable_http_path=path,
        transport_security=transport_security(),
    )
    return BearerAuth(inner), inner


def http_app():
    return build_http()[0]


def run_stdio() -> None:
    raw = os.environ.get("JOBMCP_TOKEN", "")
    with db.session() as s:
        u = auth.resolve_token(s, raw, "mcp") if raw else None
        if not u:
            raise SystemExit(
                "JOBMCP_TOKEN is missing or not a valid MCP token"
            )
        _user_id.set(u.id)
    mcp.run("stdio")
