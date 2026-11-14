"""Homerun: https://<slug>.homerun.co lists its postings as JSON inside the
<job-list> element of the page; each posting page carries its text as
JSON-LD."""

from __future__ import annotations

import json
import re
from html import unescape

from ..models import Job
from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, page_detail

PAYLOAD = re.compile(r"<job-list[^>]*\bv-bind=(['\"])(\{.*?\})\1", re.S | re.I)


class Homerun(SubdomainVendor):
    name = "homerun"
    label = "Homerun"
    example = "acme"
    hint = "It is the subdomain: <slug>.homerun.co."
    public_url_tpl = "https://{board}.homerun.co"
    base_domains = ("homerun.co",)
    uses_previous = True

    @staticmethod
    def parse_list(html: str) -> list[tuple[str, str, str]]:
        m = PAYLOAD.search(html)
        if not m:
            return []
        try:
            data = json.loads(unescape(m.group(2)).replace("\\/", "/"))
        except ValueError:
            return []
        content = data.get("content") or {}
        where = {
            loc.get("id"): (loc.get("name") or "").strip()
            for loc in content.get("locations") or []
        }
        return [
            (
                (v.get("url") or "").replace("\\/", "/"),
                unescape(v.get("title") or "").strip(),
                where.get(v.get("location_id"), ""),
            )
            for v in content.get("vacancies") or []
            if v.get("url")
        ]

    def fetch(self, board, previous=None):
        html = get_page(f"https://{board}.homerun.co/", board)
        rows = self.parse_list(html)
        if not rows and "job-list" not in html:
            raise SoftBlocked(f"{board}: not a Homerun careers page")
        listed = [
            Job(
                vendor=self.name,
                board=board,
                title=t,
                company=board,
                location=loc,
                url=u,
            )
            for u, t, loc in rows
        ]
        return enrich(listed, previous, page_detail)
