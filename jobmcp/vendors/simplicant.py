"""Simplicant: https://<slug>.simplicant.com lists the postings in HTML; each
posting page carries its text as JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, jobs_of, page_detail, text

ITEM = re.compile(
    r"<a[^>]*\blist-group-item\b[^>]*href=\"([^\"]*/jobs/\d+[^\"]*/detail)\""
    r"[^>]*>(.*?)</a>",
    re.S | re.I,
)
TITLE = re.compile(r"<h3[^>]*\bjob-title\b[^>]*>(.*?)</h3>", re.S | re.I)
SUB = re.compile(r"<div[^>]*\bjob-subtitle\b[^>]*>(.*?)</div>", re.S | re.I)
NOT_BOARDS = {"www", "assets", "app", "jobs"}


class Simplicant(SubdomainVendor):
    name = "simplicant"
    label = "Simplicant"
    example = "hoffarch"
    hint = "It is the subdomain: <slug>.simplicant.com."
    public_url_tpl = "https://{board}.simplicant.com"
    base_domains = ("simplicant.com",)
    uses_previous = True

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in NOT_BOARDS else board

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        out, seen = [], set()
        for href, body in ITEM.findall(html):
            url = urljoin(base, unescape(href))
            if url in seen:
                continue
            seen.add(url)
            title, sub = TITLE.search(body), SUB.search(body)
            out.append(
                (
                    url,
                    text(title.group(1)) if title else "",
                    text(sub.group(1)) if sub else "",
                )
            )
        return out

    def fetch(self, board, previous=None):
        base = f"https://{board}.simplicant.com/"
        html = get_page(base, board)
        rows = self.parse_list(html, base)
        if not rows and "simplicant" not in html.lower():
            raise SoftBlocked(f"{board}: not a Simplicant careers page")
        return enrich(jobs_of(self.name, board, rows), previous, page_detail)
