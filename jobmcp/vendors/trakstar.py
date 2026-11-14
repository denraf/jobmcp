"""Trakstar Hire (formerly Recruiterbox): https://<slug>.hire.trakstar.com/
jobs lists the postings in HTML; each posting page carries its text as
JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, jobs_of, page_detail, text

ITEM = re.compile(
    r"<div[^>]*\bjs-careers-page-job-list-item\b[^>]*>(.*?)"
    r"(?=<div[^>]*\bjs-careers-page-job-list-item\b|</body>|\Z)",
    re.S | re.I,
)
HREF = re.compile(r"<a[^>]*href=\"(/jobs/[^\"]+)\"", re.I)
TITLE = re.compile(
    r"<h3[^>]*\bjs-job-list-opening-name\b[^>]*>(.*?)</h3>", re.S | re.I
)
SPANS = re.compile(
    r"<span[^>]*\bmeta-job-location-(?:city|state|country)\b[^>]*>(.*?)</span>",
    re.S | re.I,
)
NOT_BOARDS = {"www", "app", "api", "login", "support"}


class Trakstar(SubdomainVendor):
    name = "trakstar"
    label = "Trakstar Hire"
    example = "trickleup"
    hint = "It is the subdomain: <slug>.hire.trakstar.com."
    public_url_tpl = "https://{board}.hire.trakstar.com/jobs"
    base_domains = (
        "hire.trakstar.com",
        "recruiterbox.com",
        "trakstarhire.com",
    )
    uses_previous = True

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in NOT_BOARDS else board

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        out, seen = [], set()
        for block in ITEM.findall(html):
            href, title = HREF.search(block), TITLE.search(block)
            if not href or not title:
                continue
            url = urljoin(base, unescape(href.group(1)))
            if url in seen:
                continue
            seen.add(url)
            places = [text(p) for p in SPANS.findall(block)]
            out.append(
                (url, text(title.group(1)), ", ".join(p for p in places if p))
            )
        return out

    def fetch(self, board, previous=None):
        base = f"https://{board}.hire.trakstar.com/jobs"
        html = get_page(base, board)
        rows = self.parse_list(html, base)
        if not rows and "trakstar" not in html.lower():
            raise SoftBlocked(f"{board}: not a Trakstar careers page")
        return enrich(jobs_of(self.name, board, rows), previous, page_detail)
