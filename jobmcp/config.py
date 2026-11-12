from __future__ import annotations

import os

DATABASE_URL = os.environ.get(
    "JOBMCP_DATABASE_URL",
    "mysql+pymysql://jobmcp:jobmcp@127.0.0.1:3306/jobmcp?charset=utf8mb4",
)
# Boards sync at most this often unless the admin sets a per-board interval.
DEFAULT_SYNC_INTERVAL_MIN = int(
    os.environ.get("JOBMCP_SYNC_INTERVAL_MIN", "60")
)
# Logging (see logs.py): DEBUG | INFO | WARNING | ERROR, text | json, and
# per-logger overrides "name=LEVEL,name=LEVEL".
LOG_LEVEL = os.environ.get("JOBMCP_LOG_LEVEL", "INFO")
LOG_FORMAT = os.environ.get("JOBMCP_LOG_FORMAT", "text")
LOG_LEVELS = os.environ.get("JOBMCP_LOG_LEVELS", "")
# Common Crawl's index server throttles hard (503/504, refused connections).
# Space requests at least this many seconds
# apart across all threads; on trouble every thread backs off together, up to 3
# minutes.
CC_MIN_INTERVAL = float(os.environ.get("JOBMCP_CC_MIN_INTERVAL", "1.0"))
# Threads reading the Common Crawl index. A pool of its own, apart from the
# vendor sync pool (SYNC_WORKERS), so a
# slow or throttled index never holds up job syncing and the reverse.
CC_WORKERS = int(os.environ.get("JOBMCP_CC_WORKERS", "3"))
# Consecutive failures (no success between) after which a crawl stops and
# resumes on the next run, rather than working through every remaining query.
CC_GIVE_UP_AFTER = int(os.environ.get("JOBMCP_CC_GIVE_UP_AFTER", "12"))
CC_RETRIES = int(os.environ.get("JOBMCP_CC_RETRIES", "8"))
# Extra boards for the daily platform canary: "vendor:board,vendor:board".
CANARY_BOARDS = os.environ.get("JOBMCP_CANARY_BOARDS", "")
# Boards checked and stored at once by a long import. Lower than SYNC_WORKERS
# on purpose: each board in flight holds all its postings in memory, and the
# biggest boards are tens of megabytes.
IMPORT_WORKERS = int(os.environ.get("JOBMCP_IMPORT_WORKERS", "6"))
# Boards fetched in parallel by one sync run (a thread pool; the work is
# network-bound).
SYNC_WORKERS = int(os.environ.get("JOBMCP_SYNC_WORKERS", "16"))
# Start syncing newly discovered boards as soon as a Common Crawl refresh
# finishes, instead of at the next hourly run.
SYNC_AFTER_REFRESH = os.environ.get("JOBMCP_SYNC_AFTER_REFRESH", "1") not in (
    "0",
    "false",
    "no",
)
# A board whose endpoint says 404/gone this many syncs in a row is disabled.
GONE_DISABLE_AFTER = int(os.environ.get("JOBMCP_GONE_DISABLE_AFTER", "3"))
MAX_BACKOFF_MIN = 24 * 60
SESSION_TTL_HOURS = int(os.environ.get("JOBMCP_SESSION_TTL_HOURS", "24"))
DEFAULT_DAILY_APPLY_CAP = int(os.environ.get("JOBMCP_DAILY_APPLY_CAP", "20"))
ALLOW_REGISTRATION = os.environ.get("JOBMCP_ALLOW_REGISTRATION", "1") not in (
    "0",
    "false",
    "no",
)
# Login/register attempts per client IP per minute (in-process; use a
# proxy-level limiter for multi-instance).
AUTH_RATE_PER_MIN = int(os.environ.get("JOBMCP_AUTH_RATE_PER_MIN", "10"))
# Only honour X-Forwarded-For when running behind a proxy you control.
TRUST_PROXY = os.environ.get("JOBMCP_TRUST_PROXY", "0") in (
    "1",
    "true",
    "yes",
)
# What the portal tells users to paste into their agent. Default assumes the
# single-process `serve`.
PUBLIC_BASE_URL = os.environ.get(
    "JOBMCP_PUBLIC_BASE_URL", "http://localhost:8000"
).rstrip("/")
PUBLIC_MCP_URL = os.environ.get(
    "JOBMCP_PUBLIC_MCP_URL", PUBLIC_BASE_URL + "/mcp/"
)

# ── email ──
# With verification on (default), signups must click an emailed link before
# they can sign in.
# Turn it off only if you have no way to send mail; then signups are verified
# immediately.
REQUIRE_EMAIL_VERIFICATION = os.environ.get(
    "JOBMCP_REQUIRE_EMAIL_VERIFICATION", "1"
) not in ("0", "false", "no")
VERIFY_TTL_HOURS = int(os.environ.get("JOBMCP_VERIFY_TTL_HOURS", "24"))
RESET_TTL_MINUTES = int(os.environ.get("JOBMCP_RESET_TTL_MINUTES", "60"))
# Minimum gap between emails of the same kind to one address (stops
# mail-bombing through the forms).
EMAIL_COOLDOWN_SECONDS = int(
    os.environ.get("JOBMCP_EMAIL_COOLDOWN_SECONDS", "60")
)
# console (log the message, for local use) | smtp | memory (tests)
MAIL_BACKEND = os.environ.get("JOBMCP_MAIL_BACKEND", "console")
MAIL_FROM = os.environ.get("JOBMCP_MAIL_FROM", "jobmcp <no-reply@localhost>")
SMTP_HOST = os.environ.get("JOBMCP_SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("JOBMCP_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("JOBMCP_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("JOBMCP_SMTP_PASSWORD", "")
SMTP_SECURITY = os.environ.get(
    "JOBMCP_SMTP_SECURITY", "starttls"
)  # starttls | ssl | none

# ── metrics ──
# /metrics is loopback-only unless a token is set (then: Authorization: Bearer
# <token>) or METRICS_PUBLIC=1.
METRICS_TOKEN = os.environ.get("JOBMCP_METRICS_TOKEN", "")
METRICS_PUBLIC = os.environ.get("JOBMCP_METRICS_PUBLIC", "0") in (
    "1",
    "true",
    "yes",
)
METRICS_CACHE_SECONDS = int(
    os.environ.get("JOBMCP_METRICS_CACHE_SECONDS", "30")
)

# How long the public jobs list is reused, in seconds, by the server and by
# browsers and proxies (Cache-Control). 0 turns it off.
CACHE_SECONDS = int(os.environ.get("JOBMCP_CACHE_SECONDS", "30"))

# Set by `jobmcp dev`; silences the production-misconfiguration warnings
# below.
DEV_MODE = False
