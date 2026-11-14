"""Getro: https://<slug>.getro.com/jobs embeds the postings of a venture
firm's portfolio in the page's __NEXT_DATA__; each posting page carries its
text as JSON-LD."""

from __future__ import annotations

import json
import re

from ..models import Job
from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, page_detail

NEXT = re.compile(
    r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>\s*(\{.*?\})\s*</script>",
    re.S | re.I,
)


class Getro(SubdomainVendor):
    name = "getro"
    label = "Getro"
    example = "acme"
    hint = "It is the subdomain: <slug>.getro.com."
    public_url_tpl = "https://{board}.getro.com/jobs"
    base_domains = ("getro.com",)
    uses_previous = True

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in ("api", "www", "blog") else board

    @staticmethod
    def found(html: str) -> list[dict] | None:
        m = NEXT.search(html)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return None
        state = (data.get("props") or {}).get("pageProps", {})
        jobs = (state.get("initialState") or {}).get("jobs") or {}
        return jobs.get("found")

    def fetch(self, board, previous=None):
        origin = f"https://{board}.getro.com"
        html = get_page(f"{origin}/jobs", board)
        found = self.found(html)
        if found is None:
            raise SoftBlocked(f"{board}: no jobs in the page")
        listed = []
        for j in found:
            org = j.get("organization") or {}
            where = (
                j.get("searchableLocations") or j.get("locations") or [""]
            )[0]
            slug = j.get("slug")
            url = (
                f"{origin}/companies/{org['slug']}/jobs/{slug}"
                if org.get("slug") and slug
                else j.get("url") or ""
            )
            if not url:
                continue
            posted = j.get("createdAt")
            listed.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=org.get("name") or board,
                    location=where,
                    url=url,
                    posted_at=posted if isinstance(posted, str) else "",
                )
            )
        return enrich(listed, previous, page_detail)
