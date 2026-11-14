"""JOIN: https://join.com/companies/<slug> embeds its postings in the page's
__NEXT_DATA__ (a few at a time, ?page=N for the rest); each posting page
carries its text as JSON-LD."""

from __future__ import annotations

import json
import re

from .. import http
from ..models import Job
from .base import BoardGone, SoftBlocked, Vendor, valid_slug
from .detail import enrich, page_detail

NEXT = re.compile(
    r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>\s*(\{.*?\})\s*</script>",
    re.S | re.I,
)
MAX_PAGES = 40


class Join(Vendor):
    name = "join"
    label = "JOIN"
    example = "novagoagency"
    hint = "It is the last part of the careers URL: join.com/companies/<slug>."
    public_url_tpl = "https://join.com/companies/{board}"
    cdx_queries = (("join.com", "domain"),)
    uses_previous = True

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host not in ("join.com", "www.join.com"):
            return None
        if len(segs) < 2 or segs[0] != "companies" or not valid_slug(segs[1]):
            return None
        return segs[1].lower()

    @staticmethod
    def page_data(html: str) -> dict | None:
        m = NEXT.search(html)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return None
        return (data.get("props") or {}).get("pageProps", {}).get("pageData")

    @staticmethod
    def _location(j: dict) -> str:
        c = j.get("city") or {}
        where = ", ".join(
            x for x in (c.get("cityName"), c.get("countryName")) if x
        )
        if (j.get("workplaceType") or "").upper() == "REMOTE":
            where = f"Remote {where}".strip()
        return where

    def fetch(self, board, previous=None):
        listed, company = [], ""
        for page in range(1, MAX_PAGES + 1):
            r = http.get(
                f"https://join.com/companies/{board}",
                params={"page": page} if page > 1 else None,
            )
            if r.status_code in (404, 410):
                raise BoardGone(board)
            r.raise_for_status()
            data = self.page_data(r.text)
            if data is None:
                raise SoftBlocked(f"{board}: no data in the page")
            company = company or (data.get("company") or {}).get("name") or ""
            jobs = data.get("jobs") or {}
            for j in jobs.get("items") or []:
                if j.get("idParam"):
                    listed.append(
                        Job(
                            vendor=self.name,
                            board=board,
                            title=(j.get("title") or "").strip(),
                            company=company or board,
                            location=self._location(j),
                            url=f"https://join.com/companies/{board}/"
                            f"{j['idParam']}",
                            posted_at=j.get("createdAt") or "",
                        )
                    )
            if page >= ((jobs.get("pagination") or {}).get("pageCount") or 1):
                break
        return enrich(listed, previous, page_detail)
