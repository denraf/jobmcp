"""Freshteam: https://<slug>.freshteam.com/jobs lists the postings; each
posting page carries its text as JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, jobs_of, page_detail, text

CARD = re.compile(
    r"<a[^>]*href=[\"'](/jobs/[^\"'#?]+(?:/[^\"'#?]+)?)[\"'][^>]*"
    r"class=[\"'][^\"']*\bheading\b[^\"']*[\"'][^>]*>(.*?)</a>",
    re.S | re.I,
)
TITLE = re.compile(r"<div[^>]*\bjob-title\b[^>]*>(.*?)</div>", re.S | re.I)
INFO = re.compile(r"<div[^>]*\blocation-info\b[^>]*>(.*?)</div>", re.S | re.I)
ATTR = re.compile(r"\bdata-portal-location=[\"']([^\"']*)[\"']", re.I)
NOT_BOARDS = {"www", "assets", "support", "api"}


class Freshteam(SubdomainVendor):
    name = "freshteam"
    label = "Freshteam"
    example = "acme"
    hint = "It is the subdomain: <slug>.freshteam.com."
    public_url_tpl = "https://{board}.freshteam.com/jobs"
    base_domains = ("freshteam.com",)
    uses_previous = True

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in NOT_BOARDS else board

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        seen, out = set(), []
        for m in CARD.finditer(html):
            url = urljoin(base, unescape(m.group(1)))
            if url in seen:
                continue
            seen.add(url)
            body = m.group(2)
            title, info = TITLE.search(body), INFO.search(body)
            attr = ATTR.search(m.group(0))
            where = (text(attr.group(1)) if attr else "") or (
                text(info.group(1)) if info else ""
            )
            out.append((url, text(title.group(1)) if title else "", where))
        return out

    def fetch(self, board, previous=None):
        base = f"https://{board}.freshteam.com/jobs"
        html = get_page(base, board)
        rows = self.parse_list(html, base)
        if not rows and "freshteam" not in html.lower():
            raise SoftBlocked(f"{board}: not a Freshteam careers page")
        return enrich(jobs_of(self.name, board, rows), previous, page_detail)
