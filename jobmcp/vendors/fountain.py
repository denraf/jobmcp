"""Fountain: web.fountain.com/c/<company>/<group>/<country>.json lists the
openings (25 a page). The listing has no description and an opening's own
page is rendered by script, so postings here have a title and a place only.

Board key: '<company>/<group>/<country>', the three parts after /c/ in the
careers URL.
"""

from __future__ import annotations

import re

from .. import http
from ..models import Job
from .base import BoardGone, Vendor, valid_slug

MAX_PAGES = 40
PART = r"[A-Za-z0-9][A-Za-z0-9._-]*"
KEY = re.compile(rf"^{PART}/{PART}/{PART}$")


class Fountain(Vendor):
    name = "fountain"
    label = "Fountain"
    example = "clear/clear/US"
    hint = (
        "It is the three parts after /c/ in the careers URL: "
        "web.fountain.com/c/<company>/<group>/<country>."
    )
    public_url_tpl = "https://web.fountain.com/c/{board}"
    cdx_queries = (("web.fountain.com", "domain"),)
    case_sensitive = True

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/")
        m = re.search(r"fountain\.com/c/([^?#]+)", s)
        s = "/".join(m.group(1).strip("/").split("/")[:3]) if m else s
        if not KEY.match(s):
            raise ValueError(
                f"{slug!r} is not a valid Fountain board name. {self.hint}"
            )
        return s

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host not in ("web.fountain.com", "www.web.fountain.com"):
            return None
        if len(segs) < 4 or segs[0] != "c":
            return None
        if not all(valid_slug(x) for x in segs[1:4]):
            return None
        return "/".join(segs[1:4])

    def fetch(self, board):
        base = f"https://web.fountain.com/c/{board}"
        listed = []
        for page in range(1, MAX_PAGES + 1):
            r = http.get(f"{base}.json", params={"page": page})
            if r.status_code in (404, 410):
                raise BoardGone(board)
            r.raise_for_status()
            data = r.json() or {}
            items = data.get("openings") or []
            for o in items:
                where = o.get("location_name") or o.get("location_address")
                listed.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(o.get("title") or "").strip(),
                        company=board.split("/")[0],
                        location=(where or "").strip(),
                        url=f"{base}/{o.get('to_param') or ''}".rstrip("/"),
                        posted_at=o.get("posted_at")
                        or o.get("created_at")
                        or "",
                    )
                )
            pages = (data.get("pagination") or {}).get("total_pages") or 1
            if page >= pages or not items:
                break
        return listed
