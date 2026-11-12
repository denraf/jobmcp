from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return "scrypt${}${}".format(
        base64.b64encode(salt).decode(), base64.b64encode(h).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, h = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=_N, r=_R, p=_P
        )
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


def new_token(kind: str) -> tuple[str, str, str]:
    """-> (raw, prefix, sha256). Tokens are high-entropy, so a fast hash is
    appropriate."""
    raw = f"lj_{'s' if kind == 'session' else 'm'}_{secrets.token_urlsafe(32)}"
    return raw, raw[:10], hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
