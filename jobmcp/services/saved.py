from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..orm import Board, Job, SavedJob
from . import ServiceError

MAX_SAVED = 1000


def save(s: Session, user_id: int, job_id: int) -> None:
    job = s.get(Job, job_id)
    if not job:
        raise ServiceError("job not found", 404)
    if s.get(SavedJob, (user_id, job_id)):
        return  # idempotent
    if (
        len(
            list(
                s.scalars(
                    select(SavedJob.job_id).where(SavedJob.user_id == user_id)
                )
            )
        )
        >= MAX_SAVED
    ):
        raise ServiceError(f"you can save at most {MAX_SAVED} jobs", 409)
    s.add(SavedJob(user_id=user_id, job_id=job_id))
    s.flush()


def unsave(s: Session, user_id: int, job_id: int) -> None:
    row = s.get(SavedJob, (user_id, job_id))
    if row:
        s.delete(row)


def list_saved(
    s: Session, user_id: int, limit: int = 50, offset: int = 0
) -> list[tuple[Job, bool]]:
    """-> [(job, still_open)], newest save first. Closed jobs stay listed so a
    save doesn't silently vanish."""
    rows = s.execute(
        select(SavedJob, Job, Board.status)
        .join(Job, Job.id == SavedJob.job_id)
        .join(Board, Board.id == Job.board_id)
        .where(SavedJob.user_id == user_id)
        .order_by(SavedJob.created_at.desc(), SavedJob.job_id.desc())
        .limit(min(limit, 200))
        .offset(offset)
    ).all()
    return [
        (j, bool(j.active and bstatus == "enabled")) for _, j, bstatus in rows
    ]


def saved_ids(s: Session, user_id: int) -> list[int]:
    return list(
        s.scalars(select(SavedJob.job_id).where(SavedJob.user_id == user_id))
    )
