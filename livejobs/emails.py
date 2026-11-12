"""Plain-text email templates. Links carry a one-time token; nothing else about
the user is included."""

from __future__ import annotations

from urllib.parse import quote

from . import config
from .mailer import Mail

SIGNOFF = "\nlivejobs finds the jobs. Your agent does the applying.\n"


def _link(path: str, token: str | None = None) -> str:
    return f"{config.PUBLIC_BASE_URL}{path}" + (
        f"?token={quote(token)}" if token else ""
    )


def verify(to: str, token: str) -> Mail:
    hours = config.VERIFY_TTL_HOURS
    return Mail(
        to,
        "Confirm your email for livejobs",
        (
            "Confirm your email address to finish creating your livejobs "
            "account:\n\n"
            f"{_link('/verify', token)}\n\n"
            f"The link works once and expires in {hours} hours.\n"
            "If you didn't sign up, ignore this email and nothing will "
            "happen.\n" + SIGNOFF
        ),
        "verify",
    )


def reset(to: str, token: str) -> Mail:
    mins = config.RESET_TTL_MINUTES
    return Mail(
        to,
        "Reset your livejobs password",
        (
            "Someone asked to reset the password for this livejobs account. "
            "To choose a new one:\n\n"
            f"{_link('/reset', token)}\n\n"
            f"The link works once and expires in {mins} minutes.\n"
            "If this wasn't you, ignore this email: your password stays as it "
            "is.\n" + SIGNOFF
        ),
        "reset",
    )


def already_registered(to: str) -> Mail:
    return Mail(
        to,
        "You already have a livejobs account",
        (
            "Someone tried to create a livejobs account with this email "
            "address, but you already have one.\n\n"
            f"Sign in:                 {_link('/login')}\n"
            f"Forgot your password?    {_link('/forgot')}\n\n"
            "If this wasn't you, you can ignore this email.\n" + SIGNOFF
        ),
        "already_registered",
    )
