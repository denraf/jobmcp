"""REST API. Session tokens only; MCP tokens are rejected here by design."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db
from . import cache, config, mailer, metrics
from .ratelimit import auth_limit, client_ip
from .security import hash_token
from .orm import ApiToken, Application, Board, InventoryRun, Job, SyncRun, User
from .services import (
    ServiceError,
    applications,
    auth,
    boards,
    imports,
    matching,
    prefs,
    saved,
    search,
)
from .services import sync as sync_svc

log = logging.getLogger("jobmcp.api")

app = FastAPI(title="jobmcp")


@app.exception_handler(ServiceError)
async def _service_error(request, exc: ServiceError):
    log.log(
        logging.WARNING if exc.code >= 500 else logging.DEBUG,
        "%s %s refused (%s): %s",
        request.method,
        request.url.path,
        exc.code,
        exc,
    )
    return JSONResponse({"detail": str(exc)}, status_code=exc.code)


def get_session():
    with db.session() as s:
        yield s


Sess = Annotated[Session, Depends(get_session)]


def current_user(
    s: Sess, authorization: str | None = Header(default=None)
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    u = auth.resolve_token(s, authorization[7:].strip(), "session")
    if not u:
        raise HTTPException(401, "invalid or expired token")
    return u


def admin_user(u: Annotated[User, Depends(current_user)]) -> User:
    if u.role != "admin":
        raise HTTPException(403, "admin only")
    return u


Me = Annotated[User, Depends(current_user)]
Admin = Annotated[User, Depends(admin_user)]


# ── serializers ─────────────────────────────────────────────────────────
def user_out(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "role": u.role,
        "is_active": u.is_active,
    }


def token_out(t: ApiToken) -> dict:
    return {
        "id": t.id,
        "kind": t.kind,
        "name": t.name,
        "prefix": t.prefix,
        "created_at": t.created_at,
        "last_used_at": t.last_used_at,
        "expires_at": t.expires_at,
    }


def job_out(j: Job, full: bool = False) -> dict:
    d = {
        "id": j.id,
        "vendor": j.vendor,
        "title": j.title,
        "company": j.company,
        "location": j.location,
        "url": j.url,
        "posted_at": j.posted_at,
        # when it sorts: the publish date, else when we first saw it
        "listed_at": j.sort_at,
        "active": j.active,
        "workplace": j.workplace,
        "seniority": j.seniority,
        "country": j.country,
        "agency": bool(j.board.is_agency),
    }
    if full:
        d["description"] = j.description
    return d


def board_out(b: Board) -> dict:
    from .vendors import VENDORS

    d = {
        c: getattr(b, c)
        for c in (
            "id",
            "vendor",
            "board",
            "status",
            "source",
            "added_by",
            "company_name",
            "sync_interval_min",
            "next_sync_at",
            "last_sync_at",
            "last_ok_at",
            "last_error",
            "consecutive_failures",
            "job_count",
            "hits",
            "is_agency",
            "agency_override",
        )
    }
    d["public_url"] = (
        VENDORS[b.vendor].public_url(b.board) if b.vendor in VENDORS else ""
    )
    return d


def app_out(a: Application) -> dict:
    return {
        "id": a.id,
        "job": job_out(a.job),
        "status": a.status,
        "method": a.method,
        "notes": a.notes,
        "applied_at": a.applied_at,
        "created_at": a.created_at,
    }


# ── auth ────────────────────────────────────────────────────────────────
class Login(BaseModel):
    email: str
    password: str


@app.post("/auth/login", dependencies=[Depends(auth_limit)])
def login(body: Login, s: Sess, request: Request):
    u = auth.authenticate(s, body.email, body.password)
    if not u:
        metrics.AUTH_EVENTS.labels("login_failed").inc()
        log.warning("login failed", extra={"ip": client_ip(request)})
        raise HTTPException(401, "invalid credentials")
    if (
        u.email_verified_at is None
        # only reachable with the right password, so it reveals nothing to a
        # stranger
    ):
        metrics.AUTH_EVENTS.labels("login_unverified").inc()
        log.info("login refused: email not confirmed", extra={"user_id": u.id})
        raise HTTPException(
            403,
            "Confirm your email address first. We sent you a link when you "
            "signed up.",
        )
    raw, t = auth.issue_token(s, u, "session", name="login")
    metrics.AUTH_EVENTS.labels("login").inc()
    log.info("login", extra={"user_id": u.id, "role": u.role})
    return {"token": raw, "expires_at": t.expires_at, "user": user_out(u)}


CHECK_EMAIL = {
    "status": "check_email",
    "message": "If that address can be used, we've sent it an email.",
}


def _deliver(s, mails) -> None:
    # the token must exist in the database before the link can be clicked
    s.commit()
    for m in mails:
        mailer.send(m)


@app.post(
    "/auth/register", status_code=202, dependencies=[Depends(auth_limit)]
)
def register(body: Login, s: Sess):
    """Self-service signup: an email and a password, nothing else. Always a
    plain `user`.

    The reply is the same whether or not the address already has an account, so
    this can't be used to
    discover who is registered. With verification off
    (JOBMCP_REQUIRE_EMAIL_VERIFICATION=0) the account is
    confirmed immediately and a session is returned instead."""
    if not config.ALLOW_REGISTRATION:
        raise HTTPException(403, "registration is closed")
    if not config.REQUIRE_EMAIL_VERIFICATION:
        u = auth.create_user(
            s, body.email, body.password, "user", verified=True
        )
        raw, t = auth.issue_token(s, u, "session", name="register")
        metrics.AUTH_EVENTS.labels("register").inc()
        log.info("registered (no email verification)", extra={"user_id": u.id})
        return JSONResponse(
            {
                "status": "signed_in",
                "token": raw,
                "expires_at": t.expires_at.isoformat(),
                "user": user_out(u),
            },
            status_code=201,
        )
    _deliver(s, auth.register(s, body.email, body.password))
    metrics.AUTH_EVENTS.labels("register").inc()
    log.info("registration submitted; confirmation email queued")
    return CHECK_EMAIL


class TokenBody(BaseModel):
    token: str


class EmailBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    password: str


@app.post("/auth/verify", dependencies=[Depends(auth_limit)])
def verify_email(body: TokenBody, s: Sess):
    auth.verify_email(s, body.token)
    metrics.AUTH_EVENTS.labels("email_verified").inc()
    log.info("email confirmed")
    return {"verified": True}


@app.post(
    "/auth/resend-verification",
    status_code=202,
    dependencies=[Depends(auth_limit)],
)
def resend_verification(body: EmailBody, s: Sess):
    _deliver(s, auth.resend_verification(s, body.email))
    return CHECK_EMAIL


@app.post("/auth/forgot", status_code=202, dependencies=[Depends(auth_limit)])
def forgot_password(body: EmailBody, s: Sess):
    _deliver(s, auth.request_reset(s, body.email))
    metrics.AUTH_EVENTS.labels("reset_requested").inc()
    log.info("password reset requested")
    return CHECK_EMAIL


@app.post("/auth/reset", status_code=204, dependencies=[Depends(auth_limit)])
def reset_password(body: ResetBody, s: Sess):
    auth.reset_password(s, body.token, body.password)
    metrics.AUTH_EVENTS.labels("password_reset").inc()
    log.info("password reset completed")


@app.post("/auth/logout", status_code=204)
def logout(u: Me, s: Sess, authorization: Annotated[str, Header()]):
    t = s.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(authorization[7:].strip())
        )
    )
    if t:
        auth.revoke_token(s, u, t.id)


@app.get("/me")
def me(u: Me):
    return user_out(u)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class PasswordConfirm(BaseModel):
    password: str


@app.put("/me/password", status_code=204)
def change_password(body: PasswordChange, u: Me, s: Sess):
    auth.change_password(s, u, body.current_password, body.new_password)


@app.post("/me/delete", status_code=204)
def delete_me(body: PasswordConfirm, u: Me, s: Sess):
    """Erases the account, preferences, saved jobs, applications ledger and
    tokens."""
    auth.delete_account(s, u, body.password)


# ── user: preferences, tokens ──────────────────────────────────
@app.get("/me/preferences")
def get_prefs(u: Me, s: Sess):
    return prefs.prefs_dict(prefs.get_preferences(s, u))


@app.put("/me/preferences")
def put_prefs(body: dict, u: Me, s: Sess):
    return prefs.prefs_dict(prefs.update_preferences(s, u, body))


class NewToken(BaseModel):
    name: str = "mcp"


@app.post("/me/mcp-tokens", status_code=201)
def create_mcp_token(body: NewToken, u: Me, s: Sess):
    """Shown once. Grants access to *this user's* MCP tools only — nothing on
    the REST API."""
    raw, t = auth.issue_token(s, u, "mcp", name=body.name)
    return {**token_out(t), "token": raw}


@app.get("/me/mcp-tokens")
def list_mcp_tokens(u: Me, s: Sess):
    return [token_out(t) for t in auth.list_tokens(s, u, "mcp")]


@app.delete("/me/mcp-tokens/{token_id}", status_code=204)
def delete_mcp_token(token_id: int, u: Me, s: Sess):
    auth.revoke_token(s, u, token_id)


# ── user: jobs & applications ───────────────────────────────────────────
@app.get("/me/matches")
def my_matches(
    u: Me, s: Sess, limit: int = Query(25, le=100), after_id: int | None = None
):
    jobs, nxt = matching.find_matches(
        s, u.id, prefs.get_preferences(s, u), limit, after_id
    )
    return {"jobs": [job_out(j) for j in jobs], "next_cursor": nxt}


def hit_out(h) -> dict:
    return {
        **job_out(h.job),
        "title_match": h.in_title,
        "snippet": h.snippet,
        "my_status": h.status,
    }


@app.get("/jobs/search")
def search_jobs(
    u: Me,
    s: Sess,
    title: str | None = None,
    keywords: str | None = None,
    match: str = "all",
    location: str | None = None,
    remote_only: bool = False,
    vendor: str | None = None,
    company: str | None = None,
    max_age_days: int | None = None,
    include_applied: bool = False,
    limit: int = Query(20, le=50),
    offset: int = 0,
    workplace: str | None = None,
    seniority: str | None = None,
    country: str | None = None,
    exclude_agencies: bool = False,
    sort: str = "relevance",
):
    """Search all open jobs by title and/or keywords in title+description. See
    services/search.py for syntax. `seniority` and `country` take
    comma-separated values (senior,lead / US,CA)."""
    r = search.search_jobs(
        s,
        u.id,
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
    return {**r, "hits": [hit_out(h) for h in r["hits"]]}


@app.get("/jobs/{job_id}")
def get_job(job_id: int, u: Me, s: Sess):
    j = s.get(Job, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return job_out(j, full=True)


class Record(BaseModel):
    status: str
    notes: str = ""


@app.get("/me/applications")
def my_applications(
    u: Me, s: Sess, status: str | None = None, limit: int = 50, offset: int = 0
):
    return [
        app_out(a)
        for a in applications.list_for_user(s, u.id, status, limit, offset)
    ]


@app.put("/me/applications/{job_id}")
def record_application(job_id: int, body: Record, u: Me, s: Sess):
    """Record an application the user made themselves (method='manual')."""
    return app_out(
        applications.record(
            s, u, job_id, body.status, body.notes, method="manual"
        )
    )


@app.get("/me/saved")
def my_saved(u: Me, s: Sess, limit: int = 50, offset: int = 0):
    return [
        {**job_out(j), "open": open_}
        for j, open_ in saved.list_saved(s, u.id, limit, offset)
    ]


@app.get("/me/saved/ids")
def my_saved_ids(u: Me, s: Sess):
    return saved.saved_ids(s, u.id)


@app.put("/me/saved/{job_id}", status_code=204)
def save_job(job_id: int, u: Me, s: Sess):
    saved.save(s, u.id, job_id)


@app.delete("/me/saved/{job_id}", status_code=204)
def unsave_job(job_id: int, u: Me, s: Sess):
    saved.unsave(s, u.id, job_id)


# ── public (no login): the jobs page and the home page ─────────────────
_stats_cache: dict = {
    "at": None,
    "v": None,
    # None = never computed (time.monotonic() counts from boot, so 0.0 is not
    # "long
    # ago")
}


@app.get("/public/config")
def public_config():
    from .vendors import VENDORS

    return {
        "registration_open": config.ALLOW_REGISTRATION,
        "mcp_url": config.PUBLIC_MCP_URL,
        "vendors": sorted(VENDORS),
    }


@app.get("/public/stats")
def public_stats(s: Sess):
    import time

    if (
        _stats_cache["at"] is None
        or time.monotonic() - _stats_cache["at"] > 60
    ):
        from sqlalchemy import func

        open_jobs = s.scalar(
            select(func.count())
            .select_from(Job)
            .join(Board, Board.id == Job.board_id)
            .where(Job.active == 1, Board.status == "enabled")
        )
        companies = s.scalar(
            select(func.count())
            .select_from(Board)
            .where(Board.status == "enabled", Board.job_count > 0)
        )
        vendors = s.scalar(
            select(func.count(func.distinct(Board.vendor))).where(
                Board.status == "enabled"
            )
        )
        _stats_cache.update(
            at=time.monotonic(),
            v={
                "open_jobs": open_jobs,
                "companies": companies,
                "platforms": vendors,
            },
        )
    return _stats_cache["v"]


@app.get("/public/jobs")
def public_jobs(
    s: Sess,
    response: Response,
    title: str | None = None,
    keywords: str | None = None,
    match: str = "all",
    location: str | None = None,
    remote_only: bool = False,
    vendor: str | None = None,
    company: str | None = None,
    max_age_days: int | None = Query(None, ge=1, le=365),
    limit: int = Query(25, ge=1, le=50),
    offset: int = Query(0, ge=0, le=5000),
    workplace: str | None = None,
    seniority: str | None = None,
    country: str | None = None,
    exclude_agencies: bool = False,
    sort: str = "newest",
):
    def compute():
        r = search.search_jobs(
            s,
            None,
            title,
            keywords,
            match,
            location,
            remote_only,
            vendor,
            company,
            max_age_days,
            True,
            limit,
            offset,
            browse=True,
            workplace=workplace,
            seniority=seniority,
            country=country,
            exclude_agencies=exclude_agencies,
            sort=sort,
        )
        return {**r, "hits": [hit_out(h) for h in r["hits"]]}

    # The same for every visitor (no user, no ledger), so one answer is shared
    # for a few seconds, and browsers and proxies may keep it that long too.
    key = (
        title,
        keywords,
        match,
        location,
        remote_only,
        vendor,
        company,
        max_age_days,
        limit,
        offset,
        workplace,
        seniority,
        country,
        exclude_agencies,
        sort,
    )
    out = cache.public.get_or_compute(key, compute, cache.ttl())
    if cache.ttl() > 0:
        response.headers["Cache-Control"] = (
            f"public, max-age={cache.ttl()}, "
            f"stale-while-revalidate={cache.ttl() * 2}"
        )
    return out


@app.get("/public/jobs/{job_id}")
def public_job(job_id: int, s: Sess, response: Response):
    j = s.get(Job, job_id)
    if not j or not j.active:
        raise HTTPException(404, "this job is no longer open")
    if config.CACHE_SECONDS > 0:  # a posting changes at most hourly
        response.headers["Cache-Control"] = (
            f"public, max-age={config.CACHE_SECONDS * 2}"
        )
    return job_out(j, full=True)


# ── admin: users ────────────────────────────────────────────────────────
class NewUser(BaseModel):
    email: str
    password: str
    role: str = "user"


class UserPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


@app.get("/admin/users")
def admin_users(_: Admin, s: Sess):
    return [user_out(u) for u in s.scalars(select(User).order_by(User.id))]


@app.post("/admin/users", status_code=201)
def admin_create_user(body: NewUser, _: Admin, s: Sess):
    return user_out(auth.create_user(s, body.email, body.password, body.role))


@app.patch("/admin/users/{user_id}")
def admin_patch_user(user_id: int, body: UserPatch, _: Admin, s: Sess):
    return user_out(
        auth.update_user(s, user_id, body.role, body.is_active, body.password)
    )


# ── admin: boards (which slugs are synced) ──────────────────────────────
class NewBoard(BaseModel):
    vendor: str | None = None
    slug: str | None = None
    url: str | None = None  # a careers page URL, instead of vendor + slug
    enable: bool = True
    sync_interval_min: int | None = None
    verify: bool = (
        True  # check that the board exists on the platform before adding it
    )


class ImportBoards(BaseModel):
    entries: list[str]
    enable: bool = True
    verify: bool = True


class BoardPatch(BaseModel):
    status: str | None = None
    sync_interval_min: int | None = None
    clear_interval: bool = False
    # True/False: an admin's call on whether the company is a staffing agency
    # (wins over the automatic guess); clear_agency goes back to automatic
    is_agency: bool | None = None
    clear_agency: bool = False


class BulkStatus(BaseModel):
    vendor: str | None = None
    from_status: str = "discovered"
    to_status: str = "enabled"
    min_hits: int = 0
    limit: int | None = None


@app.get("/admin/boards")
def admin_boards(
    _: Admin,
    s: Sess,
    status: str | None = None,
    vendor: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    source: str | None = None,
    sort: str | None = None,
    dir: str = "asc",
):
    return [
        board_out(b)
        for b in boards.list_boards(
            s, status, vendor, q, limit, offset, source, sort, dir
        )
    ]


@app.get("/admin/vendors")
def admin_vendors(_: Admin):
    """What the 'add a board' form needs: each platform, what its slug looks
    like and where to find it."""
    from .vendors import VENDORS

    return [
        {
            "vendor": v.name,
            "label": v.label or v.name,
            "example": v.example,
            "hint": v.hint,
        }
        for v in sorted(VENDORS.values(), key=lambda v: v.label or v.name)
    ]


@app.get("/admin/boards/probe")
def admin_probe(
    _: Admin,
    s: Sess,
    vendor: str | None = None,
    slug: str | None = None,
    url: str | None = None,
):
    """Check a board on its platform without adding it: does it exist, how many
    jobs, a few titles."""
    v, b = boards.resolve(vendor, slug, url)
    r = boards.public(boards.probe(v, b))
    existing = s.scalar(
        select(Board).where(Board.vendor == v, Board.board == b)
    )
    r["already"] = (
        {
            "id": existing.id,
            "status": existing.status,
            "source": existing.source,
        }
        if existing
        else None
    )
    return r


@app.post("/admin/boards", status_code=201)
def admin_add_board(body: NewBoard, admin: Admin, s: Sess):
    """Add a board by platform and slug (or a careers URL). Admin only.

    With verify (the default) the board is checked on its platform first, a
    typo is refused with a clear
    message, and the jobs found by that check are stored straight away (no
    second fetch)."""
    vendor, slug = boards.resolve(body.vendor, body.slug, body.url)
    jobs = None
    if body.verify:
        found = boards.probe(vendor, slug)
        if not found["exists"]:
            raise HTTPException(422, found["error"] or "board not found")
        jobs = found["jobs"]
    b = boards.add_board(
        s, vendor, slug, body.enable, body.sync_interval_min, added_by=admin.id
    )
    log.info(
        "board added",
        extra={
            "admin_id": admin.id,
            "vendor": vendor,
            "board": slug,
            "enabled": body.enable,
        },
    )
    if jobs is not None and body.enable:
        s.commit()
        sync_svc.sync_board(b.id, fetcher=lambda v, sl: jobs)
        s.refresh(b)
    return board_out(b)


@app.post("/admin/boards/import")
def admin_import_boards(body: ImportBoards, admin: Admin, s: Sess):
    """Add many at once. One entry per line: 'greenhouse:stripe' or a careers
    URL. Bad lines don't stop good ones.

    A short list is done within the request and answered with one result per
    line. A long one is checked, saved and
    synced in the background (a few boards at a time, so memory stays flat):
    this answers 202 with an import id, and
    GET /admin/boards/import/{id} reports progress."""
    if len([e for e in body.entries if e.strip()]) > imports.INLINE_MAX:
        job = imports.start(body.entries, body.enable, body.verify, admin.id)
        return JSONResponse(job, status_code=202)
    results = boards.add_many(
        s, body.entries, body.enable, body.verify, added_by=admin.id
    )
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log.info(
        "boards imported",
        extra={"admin_id": admin.id, "entries": len(body.entries), **counts},
    )
    return {"results": results, "summary": counts}


@app.get("/admin/boards/import/current")
def admin_import_current(_: Admin):
    return imports.current() or {}


@app.get("/admin/boards/import/{job_id}")
def admin_import_status(job_id: str, _: Admin):
    return imports.get(job_id)


@app.patch("/admin/boards/{board_id}")
def admin_patch_board(board_id: int, body: BoardPatch, admin: Admin, s: Sess):
    log.info(
        "board changed",
        extra={
            "admin_id": admin.id,
            "board_id": board_id,
            "status": body.status,
            "interval": body.sync_interval_min,
        },
    )
    return board_out(
        boards.update_board(
            s,
            board_id,
            body.status,
            body.sync_interval_min,
            body.clear_interval,
            body.is_agency,
            body.clear_agency,
        )
    )


@app.post("/admin/boards/bulk")
def admin_bulk(body: BulkStatus, admin: Admin, s: Sess):
    n = boards.bulk_set_status(
        s,
        body.vendor,
        body.from_status,
        body.to_status,
        body.min_hits,
        body.limit,
    )
    log.info(
        "boards changed in bulk",
        extra={
            "admin_id": admin.id,
            "vendor": body.vendor,
            "from": body.from_status,
            "to": body.to_status,
            "updated": n,
        },
    )
    return {"updated": n}


@app.get("/admin/health")
def admin_health(_: Admin):
    """Per platform: open postings today vs its recent range (and whether
    that looks like a break), a 30-day series, and the last live check."""
    from . import health

    return health.report()


@app.post("/admin/health/canary", status_code=202)
def admin_run_canary(admin: Admin):
    """Run the live check of every platform now, in the background. Poll
    GET /admin/health for the results."""
    from . import canary

    log.info("canary requested", extra={"admin_id": admin.id})
    return canary.start_background()


@app.post("/admin/boards/sync-all", status_code=202)
def admin_sync_all(admin: Admin):
    """Refresh every enabled board now, in the background. Poll GET
    /admin/boards/sync-all for progress."""
    log.info("sync of all boards requested", extra={"admin_id": admin.id})
    return sync_svc.start_all()


@app.get("/admin/boards/sync-all")
def admin_sync_all_status(_: Admin):
    return sync_svc.sync_all_state() or {}


@app.post("/admin/boards/{board_id}/sync")
def admin_sync_now(board_id: int, admin: Admin):
    log.info("manual sync", extra={"admin_id": admin.id, "board_id": board_id})
    return sync_svc.sync_board(board_id)


RUN_FIELDS = (
    "id",
    "crawl",
    "started_at",
    "finished_at",
    "status",
    "trigger",
    "pages_total",
    "pages_fetched",
    "pages_failed",
    "boards_new",
    "error",
)


@app.get("/admin/boards/counts")
def admin_board_counts(_: Admin, s: Sess):
    """total / enabled (syncing) / discovered (not syncing) / disabled, and
    manual (added by hand) / crawl."""
    return boards.counts(s)


@app.get("/admin/inventory")
def admin_inventory(_: Admin, s: Sess):
    """The Common Crawl refresh: whether one is running now (with progress),
    and recent runs."""
    from . import inventory

    inventory.reconcile_stale(
        s
        # a killed process leaves its run saying 'running'; don't report that
        # as
        # live
    )
    runs = list(
        s.scalars(
            select(InventoryRun).order_by(InventoryRun.id.desc()).limit(12)
        )
    )
    last_ok = next((r for r in runs if r.status == "complete"), None)
    running = next((r for r in runs if r.status == "running"), None)
    return {
        "last_complete": last_ok.finished_at if last_ok else None,
        "last_crawl": last_ok.crawl if last_ok else None,
        "running": {c: getattr(running, c) for c in RUN_FIELDS}
        if running
        else None,
        "runs": [{c: getattr(r, c) for c in RUN_FIELDS} for r in runs],
    }


@app.post("/admin/inventory/refresh")
def admin_refresh_inventory(admin: Admin):
    """Start a Common Crawl refresh now (admin only). It takes minutes to
    hours, so this returns at once and the run
    continues in the background; poll GET /admin/inventory for progress. Only
    one refresh can run at a time, across the
    web service, the scheduler and the command line."""
    from . import inventory

    r = inventory.start_background(
        trigger="web", log=logging.getLogger("jobmcp.inventory").info
    )
    crawl = r.get("crawl")
    log.info(
        "crawl refresh requested",
        extra={"admin_id": admin.id, "result": r["status"], "crawl": crawl},
    )
    if r["status"] == "started":
        return JSONResponse(
            {
                "status": "started",
                "crawl": crawl,
                "message": (
                    f"Refresh started for {crawl}. "
                    "New boards appear as it goes."
                ),
            },
            status_code=202,
        )
    if r["status"] == "up_to_date":
        return {
            "status": "up_to_date",
            "crawl": crawl,
            "message": (
                f"Nothing to do: {crawl}, the newest Common Crawl index, is "
                "already fully ingested. Common Crawl publishes a new one "
                "about once a month."
            ),
        }
    if r["status"] == "already_running":
        raise HTTPException(
            409, "A refresh is already running. Its progress is shown above."
        )
    raise HTTPException(
        502,
        f"Couldn't reach Common Crawl: {r.get('error') or 'unknown error'}",
    )


@app.get("/admin/stats")
def admin_stats(_: Admin, s: Sess):
    return boards.stats(s)


@app.get("/admin/sync-runs")
def admin_sync_runs(
    _: Admin, s: Sess, board_id: int | None = None, limit: int = 50
):
    q = select(SyncRun).order_by(SyncRun.id.desc()).limit(min(limit, 200))
    if board_id:
        q = q.where(SyncRun.board_id == board_id)
    return [
        {
            c: getattr(r, c)
            for c in (
                "id",
                "board_id",
                "started_at",
                "finished_at",
                "ok",
                "jobs_seen",
                "jobs_new",
                "jobs_closed",
                "error",
            )
        }
        for r in s.scalars(q)
    ]
