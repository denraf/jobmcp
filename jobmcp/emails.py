"""Plain-text email templates. Links carry a one-time token; nothing else about
the user is included."""

from __future__ import annotations

from urllib.parse import quote

from . import config
from .mailer import Mail

SIGNOFF = "\njobmcp finds the jobs. Your agent does the applying.\n"


def _link(path: str, token: str | None = None) -> str:
    return f"{config.PUBLIC_BASE_URL}{path}" + (
        f"?token={quote(token)}" if token else ""
    )


def verify(to: str, token: str) -> Mail:
    hours = config.VERIFY_TTL_HOURS
    return Mail(
        to,
        "Confirm your email for jobmcp",
        (
            "Confirm your email address to finish creating your jobmcp "
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
        "Reset your jobmcp password",
        (
            "Someone asked to reset the password for this jobmcp account. "
            "To choose a new one:\n\n"
            f"{_link('/reset', token)}\n\n"
            f"The link works once and expires in {mins} minutes.\n"
            "If this wasn't you, ignore this email: your password stays as it "
            "is.\n" + SIGNOFF
        ),
        "reset",
    )


def magic(to: str, token: str, next_path: str, app: str, host: str) -> Mail:
    mins = config.MAGIC_TTL_MINUTES
    sep = "&" if "?" in next_path else "?"
    link = f"{config.PUBLIC_BASE_URL}{next_path}{sep}magic={quote(token)}"
    return Mail(
        to,
        "Connect your agent to jobmcp",
        (
            f'An app called "{app}" (it will send you back to {host}) '
            "asked to use your jobmcp account. If that was you, open this "
            "link to sign in (your account is created if you don't have "
            "one) and approve it:\n\n"
            f"{link}\n\n"
            f"The link works once and expires in {mins} minutes. You get to "
            "approve or refuse before anything is shared.\n"
            "If you didn't start this, ignore this email and nothing will "
            "happen.\n" + SIGNOFF
        ),
        "magic",
    )


def already_registered(to: str) -> Mail:
    return Mail(
        to,
        "You already have a jobmcp account",
        (
            "Someone tried to create a jobmcp account with this email "
            "address, but you already have one.\n\n"
            f"Sign in:                 {_link('/login')}\n"
            f"Forgot your password?    {_link('/forgot')}\n\n"
            "If this wasn't you, you can ignore this email.\n" + SIGNOFF
        ),
        "already_registered",
    )
