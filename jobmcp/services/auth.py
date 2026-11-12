from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config, emails
from ..mailer import Mail
from ..orm import (
    ROLES,
    ApiToken,
    Application,
    EmailToken,
    Preferences,
    SavedJob,
    User,
    utcnow,
)
from ..security import hash_password, hash_token, new_token, verify_password
from . import ServiceError

log = logging.getLogger("jobmcp.auth")

MIN_PASSWORD = 10


def _clean_credentials(email: str, password: str, role: str = "user") -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 255 or any(c.isspace() for c in email):
        raise ServiceError("invalid email")
    if role not in ROLES:
        raise ServiceError(f"role must be one of {ROLES}")
    if len(password) < MIN_PASSWORD:
        raise ServiceError(
            f"password must be at least {MIN_PASSWORD} characters"
        )
    return email


def create_user(
    s: Session,
    email: str,
    password: str,
    role: str = "user",
    verified: bool = True,
) -> User:
    """Create an account. `verified=True` is for admin/CLI/internal creation;
    self-signup goes through `register`."""
    email = _clean_credentials(email, password, role)
    if s.scalar(select(User).where(User.email == email)):
        raise ServiceError("email already registered", 409)
    u = User(
        email=email,
        password_hash=hash_password(password),
        role=role,
        email_verified_at=utcnow() if verified else None,
    )
    u.preferences = Preferences(
        max_applications_per_day=config.DEFAULT_DAILY_APPLY_CAP
    )
    s.add(u)
    s.flush()
    return u


def authenticate(s: Session, email: str, password: str) -> User | None:
    u = s.scalar(select(User).where(User.email == email.strip().lower()))
    if u and verify_password(password, u.password_hash) and u.is_active:
        return u
    return None


def issue_token(
    s: Session,
    user: User,
    kind: str,
    name: str = "",
    ttl_hours: int | None = None,
) -> tuple[str, ApiToken]:
    """Returns the raw token — the only time it is ever available."""
    raw, prefix, digest = new_token(kind)
    if kind == "session" and ttl_hours is None:
        ttl_hours = config.SESSION_TTL_HOURS
    t = ApiToken(
        user_id=user.id,
        kind=kind,
        name=name,
        prefix=prefix,
        token_hash=digest,
        expires_at=utcnow() + timedelta(hours=ttl_hours)
        if ttl_hours
        else None,
    )
    s.add(t)
    s.flush()
    return raw, t


def resolve_token(s: Session, raw: str, kind: str) -> User | None:
    """A token only authenticates for its own `kind`: an MCP token can't call
    the REST API."""
    t = s.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(raw), ApiToken.kind == kind
        )
    )
    now = utcnow()
    why = None
    if not t:
        other = s.scalar(
            select(ApiToken).where(ApiToken.token_hash == hash_token(raw))
        )
        why = (
            f"this is a {other.kind} token, not a {kind} token"
            if other
            else "no such token (revoked and deleted, or from another "
            "database)"
        )
    elif t.revoked_at:
        why = f"token {t.prefix}… was revoked at {t.revoked_at}"
    elif t.expires_at and t.expires_at < now:
        why = f"token {t.prefix}… expired at {t.expires_at}"
    if why:
        log.warning(
            "%s token refused: %s (prefix %s)", kind, why, raw[:10]
        )  # the prefix is not secret; it is shown in the portal
        return None
    u = s.get(User, t.user_id)
    if not u or not u.is_active:
        log.warning(
            "%s token %s… refused: its account is missing or deactivated",
            kind,
            t.prefix,
        )
        return None
    if not t.last_used_at or (now - t.last_used_at).total_seconds() > 60:
        t.last_used_at = now
    return u


def list_tokens(
    s: Session, user: User, kind: str | None = None
) -> list[ApiToken]:
    q = select(ApiToken).where(
        ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None)
    )
    if kind:
        q = q.where(ApiToken.kind == kind)
    return list(s.scalars(q.order_by(ApiToken.id)))


def revoke_token(s: Session, user: User, token_id: int) -> None:
    t = s.get(ApiToken, token_id)
    if (
        not t or t.user_id != user.id
    ):  # other users' tokens are indistinguishable from missing
        raise ServiceError("token not found", 404)
    t.revoked_at = utcnow()


def _admin_count(s: Session) -> int:
    return len(
        list(
            s.scalars(
                select(User.id).where(
                    User.role == "admin", User.is_active.is_(True)
                )
            )
        )
    )


def update_user(
    s: Session,
    user_id: int,
    role: str | None = None,
    is_active: bool | None = None,
    password: str | None = None,
) -> User:
    u = s.get(User, user_id)
    if not u:
        raise ServiceError("user not found", 404)
    losing_admin = (
        u.role == "admin"
        and u.is_active
        and ((role is not None and role != "admin") or is_active is False)
    )
    if losing_admin and _admin_count(s) <= 1:
        raise ServiceError(
            "cannot demote or deactivate the last active admin", 409
        )
    if role is not None:
        if role not in ROLES:
            raise ServiceError(f"role must be one of {ROLES}")
        u.role = role
    if is_active is not None:
        u.is_active = is_active
    if password is not None:
        if len(password) < MIN_PASSWORD:
            raise ServiceError(
                f"password must be at least {MIN_PASSWORD} characters"
            )
        u.password_hash = hash_password(password)
    return u


def change_password(s: Session, user: User, current: str, new: str) -> None:
    if not verify_password(current, user.password_hash):
        raise ServiceError("current password is incorrect", 403)
    if len(new) < MIN_PASSWORD:
        raise ServiceError(
            f"password must be at least {MIN_PASSWORD} characters"
        )
    user.password_hash = hash_password(new)
    # other sessions die with the old password; MCP tokens are separate
    # credentials and stay
    for t in list_tokens(s, user, "session"):
        t.revoked_at = utcnow()


def delete_account(s: Session, user: User, password: str) -> None:
    """Erase the user and everything attached to them (explicit, so it is clear
    what "everything" is and
    nothing depends on cascade rules)."""
    if not verify_password(password, user.password_hash):
        raise ServiceError("password is incorrect", 403)
    if user.role == "admin" and _admin_count(s) <= 1:
        raise ServiceError("cannot delete the last active admin", 409)
    for model in (ApiToken, EmailToken, Application, SavedJob, Preferences):
        s.execute(delete(model).where(model.user_id == user.id))
    s.delete(user)


# ── email verification & password reset ─────────────────────────────────
def _issue(s: Session, user: User, purpose: str, ttl: timedelta) -> str | None:
    """A fresh single-use token, replacing any unused one. None if one was sent
    within the cooldown
    (so the forms can't be used to flood someone's inbox)."""
    now = utcnow()
    open_tokens = list(
        s.scalars(
            select(EmailToken).where(
                EmailToken.user_id == user.id,
                EmailToken.purpose == purpose,
                EmailToken.used_at.is_(None),
            )
        )
    )
    if any(
        (now - t.created_at).total_seconds() < config.EMAIL_COOLDOWN_SECONDS
        for t in open_tokens
    ):
        return None
    for t in open_tokens:
        t.used_at = now  # the old link stops working
    raw = secrets.token_urlsafe(32)
    s.add(
        EmailToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(raw),
            expires_at=now + ttl,
        )
    )
    s.flush()
    return raw


def _consume(s: Session, raw: str, purpose: str) -> User:
    t = s.scalar(
        select(EmailToken).where(
            EmailToken.token_hash == hash_token(raw or ""),
            EmailToken.purpose == purpose,
        )
    )
    now = utcnow()
    if not t or t.used_at or t.expires_at < now:
        raise ServiceError("this link is invalid or has expired", 400)
    u = s.get(User, t.user_id)
    if not u or not u.is_active:
        raise ServiceError("this link is invalid or has expired", 400)
    t.used_at = now
    return u


def _verify_mail(s: Session, user: User) -> Mail | None:
    raw = _issue(s, user, "verify", timedelta(hours=config.VERIFY_TTL_HOURS))
    return emails.verify(user.email, raw) if raw else None


def register(s: Session, email: str, password: str) -> list[Mail]:
    """Self-signup. Returns the emails to send (after commit). The caller must
    answer identically
    whether or not the address already has an account, so this can't be used to
    find out who is registered."""
    email = _clean_credentials(email, password)
    u = s.scalar(select(User).where(User.email == email))
    if u is None:
        u = create_user(
            s, email, password, verified=not config.REQUIRE_EMAIL_VERIFICATION
        )
        return (
            [m for m in [_verify_mail(s, u)] if m]
            if config.REQUIRE_EMAIL_VERIFICATION
            else []
        )
    if u.email_verified_at is None:
        u.password_hash = hash_password(
            password
        )  # the latest signup attempt wins until the address is confirmed
        return [m for m in [_verify_mail(s, u)] if m]
    return [emails.already_registered(u.email)]


def verify_email(s: Session, raw: str) -> None:
    u = _consume(s, raw, "verify")
    if u.email_verified_at is None:
        u.email_verified_at = utcnow()


def resend_verification(s: Session, email: str) -> list[Mail]:
    u = s.scalar(select(User).where(User.email == email.strip().lower()))
    if u and u.is_active and u.email_verified_at is None:
        return [m for m in [_verify_mail(s, u)] if m]
    return []


def request_reset(s: Session, email: str) -> list[Mail]:
    u = s.scalar(select(User).where(User.email == email.strip().lower()))
    if not u or not u.is_active:
        return []
    raw = _issue(s, u, "reset", timedelta(minutes=config.RESET_TTL_MINUTES))
    return [emails.reset(u.email, raw)] if raw else []


def reset_password(s: Session, raw: str, new_password: str) -> None:
    if (
        len(new_password) < MIN_PASSWORD
    ):  # check first so a typo doesn't burn the single-use link
        raise ServiceError(
            f"password must be at least {MIN_PASSWORD} characters"
        )
    u = _consume(s, raw, "reset")
    u.password_hash = hash_password(new_password)
    if u.email_verified_at is None:
        u.email_verified_at = (
            utcnow()
        )  # they just proved they control the inbox
    now = utcnow()
    for t in list_tokens(
        s, u, "session"
    ):  # sign out everywhere; agent (MCP) tokens are left alone
        t.revoked_at = now
    for t in s.scalars(
        select(EmailToken).where(
            EmailToken.user_id == u.id, EmailToken.used_at.is_(None)
        )
    ):
        # any other outstanding link (e.g. an older reset email) dies too
        t.used_at = now
