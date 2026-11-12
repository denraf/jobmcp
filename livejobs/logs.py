"""Logging setup, driven by the environment so a container needs no config
file.

Everything goes to stdout, one line per record, which is what `docker logs` /
`container logs` / journald collect.

  LIVEJOBS_LOG_LEVEL    DEBUG | INFO | WARNING | ERROR   (default INFO)
  LIVEJOBS_LOG_FORMAT   text | json                      (default text; json is
  one object per line for log shippers)
  LIVEJOBS_LOG_LEVELS   per-logger overrides, e.g.
  "livejobs.sync=DEBUG,livejobs.http=DEBUG,sqlalchemy.engine=INFO"

Secrets are never logged: no passwords, tokens, query strings or message
bodies. Users appear as ids, not addresses.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys

from . import config

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "request_id",
    "taskName",
}
_NOISY = {
    "urllib3": "WARNING",
    "httpx": "WARNING",
    "httpx2": "WARNING",
    "httpcore": "WARNING",
    "asyncio": "WARNING",
    "multipart": "WARNING",
    "watchfiles": "WARNING",
}


class _Context(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__(
            "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s]%(rid)s "
            "%(message)s",
            "%Y-%m-%dT%H:%M:%S%z",
        )

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", "-")
        record.rid = f" req={rid}" if rid != "-" else ""
        extra = " ".join(
            f"{k}={v}"
            for k, v in record.__dict__.items()
            if k not in _STD and k != "rid"
        )
        out = super().format(record)
        return f"{out} {extra}" if extra else out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        if getattr(record, "request_id", "-") != "-":
            d["request_id"] = record.request_id
        d.update({k: v for k, v in record.__dict__.items() if k not in _STD})
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, default=str)


def parse_level(value: str | None, default: str = "INFO") -> int:
    name = (value or default).strip().upper()
    if name == "WARN":
        name = "WARNING"
    if name not in LEVELS:
        raise ValueError(
            f"log level {value!r} is not one of {', '.join(LEVELS)}"
        )
    return getattr(logging, name)


def parse_overrides(spec: str | None) -> dict[str, int]:
    out = {}
    for part in (spec or "").split(","):
        if part.strip():
            name, _, level = part.partition("=")
            if not name.strip() or not level:
                raise ValueError(
                    f"LIVEJOBS_LOG_LEVELS entry {part!r} must look like "
                    "logger.name=LEVEL"
                )
            out[name.strip()] = parse_level(level)
    return out


def setup(
    level: str | None = None,
    fmt: str | None = None,
    overrides: str | None = None,
    stream=None,
) -> None:
    """Configure the root logger. Arguments beat the environment. Safe to call
    more than once."""
    try:
        lvl = parse_level(level or config.LOG_LEVEL)
        per = parse_overrides(
            overrides if overrides is not None else config.LOG_LEVELS
        )
    except ValueError as e:
        print(f"livejobs: {e}", file=sys.stderr)
        raise SystemExit(2)
    fmt = (fmt or config.LOG_FORMAT or "text").lower()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(_Context())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_livejobs", False):
            root.removeHandler(h)
    handler._livejobs = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(lvl)
    # chatty libraries stay quiet unless asked for by name in
    # LIVEJOBS_LOG_LEVELS
    for name, noisy_level in _NOISY.items():
        logging.getLogger(name).setLevel(noisy_level)
    # uvicorn would install its own handlers and format; let its records flow
    # through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(
        logging.WARNING
    )  # our request middleware logs requests, with timings
    for name, override in per.items():
        logging.getLogger(name).setLevel(override)
    logging.getLogger("livejobs").debug(
        "logging ready: level=%s format=%s overrides=%s",
        logging.getLevelName(lvl),
        fmt,
        per or "none",
    )


def configured() -> bool:
    return any(
        getattr(h, "_livejobs", False) for h in logging.getLogger().handlers
    )
