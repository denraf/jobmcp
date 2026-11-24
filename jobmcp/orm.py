"""MySQL 8 schema (SQLAlchemy 2.x)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

LongText = MEDIUMTEXT()  # job descriptions: TEXT would cut at 64 KB
# Board slugs are case-sensitive on some vendors (Ashby, SmartRecruiters), so
# MySQL must not fold
# 'Acme' and 'acme' together under its default case-insensitive collation.
Slug = String(255, collation="utf8mb4_bin")
PK = BigInteger()

ROLES = ("admin", "user")
BOARD_STATUSES = ("discovered", "enabled", "disabled")
APPLICATION_STATUSES = (
    "in_progress",
    "applied",
    "failed",
    "skipped",
    "interview",
    "offer",
    "rejected",
)


def utcnow() -> datetime:
    """Naive UTC, whole seconds. MySQL DATETIME has no tz and *rounds*
    fractional seconds (possibly
    into the future), which would make a just-written `next_sync_at = now` not
    yet due."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    preferences: Mapped["Preferences | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class EmailToken(Base):
    """Single-use links emailed to a user: purpose 'verify' (confirm the
    address) or 'reset' (new password)."""

    __tablename__ = "email_tokens"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True
    )  # sha256; the raw token only ever lives in the email
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiToken(Base):
    """kind='session' -> REST API, expiring. kind='mcp' -> MCP server only,
    long-lived."""

    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(100), default="")
    prefix: Mapped[str] = mapped_column(
        String(12)
    )  # shown in listings so users can tell tokens apart
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True
    )  # sha256 hex; the raw token is never stored
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # Set on the short-lived access tokens an agent obtains by signing in
    # (OAuth, see services/oauth.py): the approval they belong to. Such
    # tokens are not listed with the ones a person copies by hand.
    grant_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OAuthClient(Base):
    """An agent application that registered itself (RFC 7591). Public client:
    it has no secret, PKCE protects its codes."""

    __tablename__ = "oauth_clients"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    redirect_uris: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OAuthGrant(Base):
    """One approval: this user let this client use their jobmcp account. The
    unit the user sees and can revoke."""

    __tablename__ = "oauth_grants"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )


class OAuthCode(Base):
    """A one-time authorization code, valid for a minute. Stored hashed."""

    __tablename__ = "oauth_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[int] = mapped_column(
        ForeignKey("oauth_grants.id", ondelete="CASCADE")
    )
    redirect_uri: Mapped[str] = mapped_column(String(500))
    code_challenge: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OAuthRefresh(Base):
    """A refresh token, stored hashed. Each use hands out a new one; using an
    old one again means it leaked, so the whole grant is revoked."""

    __tablename__ = "oauth_refresh"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[int] = mapped_column(
        ForeignKey("oauth_grants.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Preferences(Base):
    __tablename__ = "preferences"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    title_include: Mapped[list] = mapped_column(
        JSON, default=list
    )  # any-of; empty = match nothing
    title_exclude: Mapped[list] = mapped_column(JSON, default=list)
    locations: Mapped[list] = mapped_column(
        JSON, default=list
    )  # any-of substrings; empty = anywhere
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False)
    vendors: Mapped[list] = mapped_column(JSON, default=list)  # empty = all
    companies_include: Mapped[list] = mapped_column(JSON, default=list)
    companies_exclude: Mapped[list] = mapped_column(JSON, default=list)
    description_exclude: Mapped[list] = mapped_column(JSON, default=list)
    max_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # any-of seniority tiers (see classify.SENIORITY); empty = any
    seniority: Mapped[list] = mapped_column(JSON, default=list)
    # any-of ISO country codes; empty = anywhere
    countries: Mapped[list] = mapped_column(JSON, default=list)
    exclude_agencies: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0")
    )
    # 'offered' = only jobs that say they sponsor a visa; 'not_excluded' =
    # also jobs that are silent about it; '' = no preference
    visa: Mapped[str] = mapped_column(
        String(12), default="", server_default=""
    )
    max_applications_per_day: Mapped[int] = mapped_column(Integer, default=20)
    user: Mapped[User] = relationship(back_populates="preferences")


class Board(Base):
    __tablename__ = "boards"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    vendor: Mapped[str] = mapped_column(String(32))
    board: Mapped[str] = mapped_column(Slug)
    # discovered = found by Common Crawl, not synced until an admin enables it
    status: Mapped[str] = mapped_column(String(16), default="discovered")
    # crawl = discovered by Common Crawl; manual = an admin added (or adopted)
    # it by hand
    source: Mapped[str] = mapped_column(String(16), default="crawl")
    added_by: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # user id of the admin, for manual boards
    company_name: Mapped[str] = mapped_column(String(255), default="")
    # looks like a staffing agency (classify.is_agency), unless an admin
    # decided: agency_override True/False wins over the automatic guess
    is_agency: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0")
    )
    agency_override: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    sync_interval_min: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None -> global default
    next_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_ok_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_gone: Mapped[int] = mapped_column(Integer, default=0)
    job_count: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[int] = mapped_column(
        Integer, default=1
    )  # Common Crawl URL count: a rough popularity signal
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("vendor", "board"),
        Index("ix_boards_due", "status", "next_sync_at"),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(16), unique=True)
    board_id: Mapped[int] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE")
    )
    vendor: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500))
    company: Mapped[str] = mapped_column(String(255), default="")
    location: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    description: Mapped[str] = mapped_column(LongText, default="")
    # labels derived from the title and location (classify.py), '' = unknown
    workplace: Mapped[str] = mapped_column(
        String(8), default="", server_default=""
    )
    seniority: Mapped[str] = mapped_column(
        String(12), default="", server_default=""
    )
    country: Mapped[str] = mapped_column(
        String(2), default="", server_default=""
    )
    # Pay stated in the posting (salary.py). Amounts are per year in
    # salary_currency; salary_period is the period it was quoted in (a rate
    # of $45 an hour is stored as a year and shown as /hr again). The USD
    # pair is approximate and only places a posting in a salary band.
    # NULL = the posting states no pay.
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_currency: Mapped[str] = mapped_column(
        String(3), default="", server_default=""
    )
    salary_period: Mapped[str] = mapped_column(
        String(5), default="", server_default=""
    )
    # Visa sponsorship as the description states it (classify.visa): 'yes'
    # offers it, 'no' rules it out, '' = not stated.
    visa: Mapped[str] = mapped_column(String(3), default="", server_default="")
    salary_usd_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_usd_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # What lists sort by: when the company says it was published, else when
    # we first saw it, never later than now. A stored, indexed column because
    # ordering 300k rows by COALESCE(posted_at, first_seen) takes seconds.
    sort_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    board: Mapped[Board] = relationship()
    __table_args__ = (
        Index("ix_jobs_board_active", "board_id", "active"),
        Index("ix_jobs_active_posted", "active", "posted_at"),
        Index("ix_jobs_active_sort", "active", "sort_at", "id"),
        # Word search over millions of postings (services/search.py). Three
        # indexes because MATCH must name exactly the columns of one index:
        # title+company (weighted up when ranking), company alone (the
        # company filter) and title+company+description (keywords).
        Index("ft_jobs_title", "title", "company", mysql_prefix="FULLTEXT"),
        Index("ft_jobs_company", "company", mysql_prefix="FULLTEXT"),
        Index(
            "ft_jobs_text",
            "title",
            "company",
            "description",
            mysql_prefix="FULLTEXT",
        ),
        # Cover the jobs-page filters and their counts: MySQL answers from
        # the index and never reads the (large) description rows.
        Index("ix_jobs_active_workplace", "active", "workplace", "board_id"),
        Index("ix_jobs_active_seniority", "active", "seniority", "board_id"),
        Index("ix_jobs_active_country", "active", "country", "board_id"),
        Index("ix_jobs_active_visa", "active", "visa", "board_id"),
        Index(
            "ix_jobs_active_salary",
            "active",
            "salary_usd_min",
            "salary_usd_max",
            "board_id",
        ),
        # The jobs list walks the newest postings and checks every filter:
        # all of them are in this index, so that walk never reads a row.
        Index(
            "ix_jobs_browse",
            "active",
            "sort_at",
            "id",
            "board_id",
            "workplace",
            "seniority",
            "country",
            "salary_usd_min",
            "salary_usd_max",
        ),
    )


class Application(Base):
    """One row per (user, job): the ledger of what each user has applied to."""

    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), default="in_progress")
    method: Mapped[str] = mapped_column(
        String(16), default="mcp"
    )  # mcp | manual
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    job: Mapped[Job] = relationship()
    __table_args__ = (
        UniqueConstraint("user_id", "job_id"),
        Index("ix_app_user_status", "user_id", "status"),
    )


class SavedJob(Base):
    """A bookmark. Deliberately holds nothing but the job id: no notes, no
    personal data."""

    __tablename__ = "saved_jobs"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    job: Mapped[Job] = relationship()


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    board_id: Mapped[int] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_closed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class InventoryRun(Base):
    """One attempt to ingest a Common Crawl index: the audit trail for the
    monthly refresh."""

    __tablename__ = "inventory_runs"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    crawl: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="running"
    )  # running | complete | partial | failed
    trigger: Mapped[str] = mapped_column(
        String(16), default="scheduled"
    )  # scheduled | web | cli
    pages_total: Mapped[int] = mapped_column(
        Integer, default=0
    )  # pages this run has found to fetch so far
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0)
    boards_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CrawlProgress(Base):
    __tablename__ = "crawl_progress"
    crawl: Mapped[str] = mapped_column(String(32), primary_key=True)
    vendor: Mapped[str] = mapped_column(String(32), primary_key=True)
    query: Mapped[str] = mapped_column(String(128), primary_key=True)
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    num_pages: Mapped[int] = mapped_column(Integer)


class VendorSnapshot(Base):
    """One row per platform per day: how many postings it had. The history
    behind the 'a platform just lost most of its jobs' alert and the trend
    chart. The latest row of the day is overwritten as syncs finish."""

    __tablename__ = "vendor_snapshots"
    day: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    vendor: Mapped[str] = mapped_column(String(32), primary_key=True)
    active_jobs: Mapped[int] = mapped_column(Integer, default=0)
    boards_enabled: Mapped[int] = mapped_column(Integer, default=0)
    boards_failing: Mapped[int] = mapped_column(Integer, default=0)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CanaryResult(Base):
    """The latest live check of one platform: did a known board still parse
    into real postings? Catches a vendor changing its markup or API."""

    __tablename__ = "canary_results"
    vendor: Mapped[str] = mapped_column(String(32), primary_key=True)
    board: Mapped[str] = mapped_column(Slug, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    jobs: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    """Work the web service asks the worker service to do (imports, syncs,
    the platform check, the Common Crawl refresh). The web service only
    inserts a row and reads progress back; the worker claims rows, does the
    work and records progress. See services/tasks.py."""

    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    # queued -> running -> done | failed
    state: Mapped[str] = mapped_column(String(12), default="queued")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set to the kind while a one-at-a-time task is queued or running, NULL
    # once it ends. The unique index is what makes "already running" safe when
    # two web workers get the same click.
    active_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    worker: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    __table_args__ = (
        UniqueConstraint("active_key", name="uq_tasks_active_key"),
        Index("ix_tasks_state_id", "state", "id"),
        Index("ix_tasks_kind_id", "kind", "id"),
    )
