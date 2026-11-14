"""Comeet: https://www.comeet.com/jobs/<name>/<id> lists a company's postings
as `COMPANY_POSITIONS_DATA = [...]`, text included.

Board key: '<name>/<id>', the two parts after /jobs/ in the careers URL.
"""

from __future__ import annotations

import json
import re

from ..models import Job, clean_html
from .base import SoftBlocked, Vendor, valid_slug
from .detail import get_page

DATA = re.compile(r"COMPANY_POSITIONS_DATA\s*=\s*")
HOSTS = ("www.comeet.com", "comeet.com")
KEY = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)$"
)


class Comeet(Vendor):
    name = "comeet"
    label = "Comeet"
    example = "biocatch/03.00E"
    hint = (
        "It is the two parts after /jobs/ in the careers URL: "
        "comeet.com/jobs/<name>/<id> -> <name>/<id>."
    )
    public_url_tpl = "https://www.comeet.com/jobs/{board}"
    cdx_queries = (("www.comeet.com", "domain"),)
    case_sensitive = True  # the id is case-sensitive

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/")
        m = re.search(r"comeet\.com/jobs/([^/?#]+/[^/?#]+)", s)
        s = m.group(1) if m else s
        if not KEY.match(s):
            raise ValueError(
                f"{slug!r} is not a valid Comeet board name. {self.hint}"
            )
        return s

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host not in HOSTS or len(segs) < 3 or segs[0] != "jobs":
            return None
        if not (valid_slug(segs[1]) and valid_slug(segs[2])):
            return None
        return f"{segs[1].lower()}/{segs[2]}"

    @staticmethod
    def positions(html: str) -> list[dict] | None:
        m = DATA.search(html)
        if not m:
            return None
        try:
            obj, _ = json.JSONDecoder().raw_decode(html, m.end())
        except ValueError:
            return None
        return obj if isinstance(obj, list) else None

    @staticmethod
    def _description(p: dict) -> str:
        cf = p.get("custom_fields") or {}
        parts = []
        for d in cf.get("details") or []:
            body = clean_html(d.get("value"))
            if body:
                name = (d.get("name") or "").strip()
                parts.append(
                    f"{name}\n{body}"
                    if name and name != "Description"
                    else body
                )
        return "\n\n".join(parts)

    @staticmethod
    def _location(p: dict) -> str:
        loc = p.get("location") or {}
        if isinstance(loc, str):
            return loc
        where = loc.get("name") or ", ".join(
            x for x in (loc.get("city"), loc.get("country")) if x
        )
        kind = p.get("workplace_type")
        if kind == "Remote" and "remote" not in where.lower():
            where = f"Remote {where}".strip()
        return where

    def fetch(self, board):
        html = get_page(f"https://www.comeet.com/jobs/{board}", board)
        items = self.positions(html)
        if items is None:
            raise SoftBlocked(f"{board}: no positions in the page")
        out = []
        for p in items:
            url = (
                p.get("url_comeet_hosted_page")
                or p.get("url_recruit_hosted_page")
                or p.get("url_active_page")
                or p.get("url_detected_page")
                or ""
            )
            if not url:
                continue
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(p.get("name") or "").strip(),
                    company=p.get("company_name") or board.split("/")[0],
                    location=self._location(p),
                    url=url,
                    posted_at=p.get("time_updated") or "",
                    description=self._description(p),
                )
            )
        return out
