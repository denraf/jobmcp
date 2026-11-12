"""The per-user ledger of applications. One row per (user, job), enforced by a
unique key."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..orm import APPLICATION_STATUSES, Application, Board, Job, utcnow
from . import ServiceError
from .prefs import get_preferences

# applied -> in_progress would let a retry double-apply, so only these end
# states are terminal for `start`
TERMINAL = {"applied", "interview", "offer", "rejected", "skipped"}
COUNTS_TOWARD_CAP = (
    "in_progress",
    "applied",
    "interview",
    "offer",
    "rejected",
)


def applications_today(s: Session, user_id: int) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return len(
        list(
            s.scalars(
                select(Application.id).where(
                    Application.user_id == user_id,
                    Application.created_at >= start,
                    Application.status.in_(COUNTS_TOWARD_CAP),
                )
            )
        )
    )


def start(s: Session, user, job_id: int) -> tuple[Application, bool]:
    """Claim a job before applying. -> (application, resumed).

    Raises if the job is closed, already applied/skipped, or the daily cap is
    reached.
    A leftover in_progress row (an earlier agent run died) is returned with
    resumed=True.
    """
    job = s.get(Job, job_id)
    if (
        not job
        or not job.active
        or s.get(Board, job.board_id).status != "enabled"
    ):
        raise ServiceError("job not found or no longer open", 404)
    app = s.scalar(
        select(Application).where(
            Application.user_id == user.id, Application.job_id == job_id
        )
    )
    if app:
        if app.status == "in_progress":
            return app, True
        if app.status == "failed":
            app.status = "in_progress"
            return app, True
        raise ServiceError(f"already {app.status}", 409)
    cap = get_preferences(s, user).max_applications_per_day
    if applications_today(s, user.id) >= cap:
        raise ServiceError(
            f"daily application cap reached ({cap}); "
            "raise max_applications_per_day to continue",
            429,
        )
    app = Application(
        user_id=user.id, job_id=job_id, status="in_progress", method="mcp"
    )
    try:
        with s.begin_nested():
            s.add(app)
            s.flush()
    except IntegrityError:  # a concurrent agent claimed it first
        raise ServiceError("already claimed", 409)
    return app, False


def record(
    s: Session,
    user,
    job_id: int,
    status: str,
    notes: str = "",
    method: str = "mcp",
) -> Application:
    """Set the outcome. Creates the row if the user applied outside the claim
    flow (method='manual')."""
    if status not in APPLICATION_STATUSES:
        raise ServiceError(f"status must be one of {APPLICATION_STATUSES}")
    if not s.get(Job, job_id):
        raise ServiceError("job not found", 404)
    app = s.scalar(
        select(Application).where(
            Application.user_id == user.id, Application.job_id == job_id
        )
    )
    if app is None:
        app = Application(user_id=user.id, job_id=job_id, method=method)
        s.add(app)
    elif app.status in TERMINAL and status in ("in_progress", "failed"):
        raise ServiceError(f"already {app.status}; refusing to downgrade", 409)
    app.status = status
    if notes:
        app.notes = notes[:5000]
    if status == "applied" and not app.applied_at:
        app.applied_at = utcnow()
    s.flush()
    return app


def list_for_user(
    s: Session,
    user_id: int,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Application]:
    q = select(Application).where(Application.user_id == user_id)
    if status:
        q = q.where(Application.status == status)
    return list(
        s.scalars(
            q.order_by(Application.id.desc())
            .limit(min(limit, 200))
            .offset(offset)
        )
    )
