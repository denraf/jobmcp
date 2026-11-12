"""Paylocity Recruiting: the tenant's job list page embeds its postings as a
JSON object (`window.pageData = {...}`).

Board key: the tenant's GUID (lower case), the part after /jobs/All/ in the
careers URL. Paylocity does not use readable names.
"""

from __future__ import annotations

import json
import re
from html import unescape

from .. import http
from ..models import Job, clean_html
from .base import SoftBlocked, Vendor

GUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
HOST = "recruiting.paylocity.com"
MARK = "window.pageData"


class Paylocity(Vendor):
    name = "paylocity"
    label = "Paylocity"
    example = "9597a587-1c23-4def-9ecc-4901e647fab7"
    hint = (
        "It is the GUID in the careers URL: "
        "recruiting.paylocity.com/recruiting/jobs/All/<guid>/..."
    )
    public_url_tpl = (
        "https://recruiting.paylocity.com/recruiting/jobs/All/{board}"
    )
    cdx_queries = ((HOST, "domain"),)

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/").lower()
        m = re.search(r"/jobs/all/([0-9a-f-]{36})", s)
        s = m.group(1) if m else s
        if not GUID.match(s):
            raise ValueError(
                f"{slug!r} is not a valid Paylocity board name. {self.hint}"
            )
        return s

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != HOST:
            return None
        low = [s.lower() for s in segs]
        # /recruiting/jobs/All/<guid>[/<name>]  (also /Career/ and /Details/
        # are skipped: they carry no tenant GUID we can rely on)
        if len(low) >= 4 and low[:3] == ["recruiting", "jobs", "all"]:
            return low[3] if GUID.match(low[3]) else None
        return None

    @staticmethod
    def page_data(html: str) -> dict | None:
        """The object assigned to window.pageData, or None. Decoded by the
        JSON parser from the opening brace, so nested braces and script
        endings inside strings cannot cut it short."""
        i = html.find(MARK)
        if i < 0:
            return None
        j = html.find("{", i)
        if j < 0:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(html[j:])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def fetch(self, board):
        r = http.get(f"https://{HOST}/recruiting/jobs/All/{board}/")
        r.raise_for_status()
        data = self.page_data(r.text)
        if data is None or "Jobs" not in data:
            # 200 but no job list: a block page or a changed layout. Not
            # the same thing as a company with no openings.
            raise SoftBlocked(f"{board}: no job list in the page")
        company = unescape(str(data.get("CompanyName") or "")).strip()
        out = []
        for j in data.get("Jobs") or []:
            jid = j.get("JobId")
            if not jid:
                continue
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=unescape(j.get("JobTitle") or "").strip(),
                    company=company or board,
                    location=_location(j),
                    url=f"https://{HOST}/Recruiting/Jobs/Details/{jid}",
                    posted_at=str(j.get("PublishedDate") or ""),
                    # the list carries the start of the description only
                    description=clean_html(j.get("Description")),
                )
            )
        return out


def _location(j: dict) -> str:
    loc = j.get("JobLocation") or {}
    bits = [loc.get("City"), loc.get("State"), loc.get("Country")]
    text = ", ".join(str(b) for b in bits if b)
    if j.get("IsRemote"):
        text = ("Remote " + text).strip()
    return text or (j.get("LocationName") or "")
