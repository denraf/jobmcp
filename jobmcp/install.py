"""One-click install links for agents that have them (Cursor, VS Code)."""

from __future__ import annotations

import base64
import json
from urllib.parse import quote

NAME = "jobmcp"


def cursor_link(mcp_url: str) -> str:
    cfg = json.dumps({"url": mcp_url}, separators=(",", ":"))
    b64 = base64.b64encode(cfg.encode()).decode()
    return (
        "cursor://anysphere.cursor-deeplink/mcp/install"
        f"?name={NAME}&config={quote(b64, safe='')}"
    )


def vscode_link(mcp_url: str) -> str:
    cfg = json.dumps(
        {"name": NAME, "type": "http", "url": mcp_url}, separators=(",", ":")
    )
    return "vscode:mcp/install?" + quote(cfg, safe="")


def links(mcp_url: str) -> dict[str, str]:
    return {"cursor": cursor_link(mcp_url), "vscode": vscode_link(mcp_url)}
