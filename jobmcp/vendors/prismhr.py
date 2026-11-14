"""PrismHR Hire: https://<slug>.prismhr-hire.com lists the postings in HTML;
each posting page carries its text as JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, jobs_of, page_detail, text

LINK = re.compile(
    r"<a[^>]*href=[\"'](/job/\d+/[^\"']+)[\"'][^>]*>\s*<h2>(.*?)</h2>",
    re.S | re.I,
)
WHERE = re.compile(r"<div[^>]*\bjob-location\b[^>]*>(.*?)</div>", re.S | re.I)
NOT_BOARDS = {"www", "login", "api"}


class PrismHR(SubdomainVendor):
    name = "prismhr"
    label = "PrismHR Hire"
    example = "que-chido-llc"
    hint = "It is the subdomain: <slug>.prismhr-hire.com."
    public_url_tpl = "https://{board}.prismhr-hire.com"
    base_domains = ("prismhr-hire.com",)
    uses_previous = True

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in NOT_BOARDS else board

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        found = list(LINK.finditer(html))
        out, seen = [], set()
        for m, nxt in zip(found, found[1:] + [None]):
            url = urljoin(base, unescape(m.group(1)))
            if url in seen:
                continue
            seen.add(url)
            block = html[m.end() : nxt.start() if nxt else len(html)]
            where = WHERE.search(block)
            out.append(
                (url, text(m.group(2)), text(where.group(1)) if where else "")
            )
        return out

    def fetch(self, board, previous=None):
        base = f"https://{board}.prismhr-hire.com/"
        html = get_page(base, board)
        rows = self.parse_list(html, base)
        if not rows and "prismhr" not in html.lower():
            raise SoftBlocked(f"{board}: not a PrismHR careers page")
        return enrich(jobs_of(self.name, board, rows), previous, page_detail)
