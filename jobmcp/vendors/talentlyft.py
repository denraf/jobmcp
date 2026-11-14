"""TalentLyft: https://<slug>.talentlyft.com loads its postings as an HTML
fragment from /JobList/ (20 a page); each posting page carries its text as
JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .. import http
from ..models import Job
from .base import BoardGone, SubdomainVendor
from .detail import enrich, page_detail, text

BOX = re.compile(
    r"<a[^>]*\bclass=['\"][^'\"]*\bjobs__box\b[^'\"]*['\"][^>]*>.*?</a>",
    re.S | re.I,
)
HREF = re.compile(r"\bhref=['\"]([^'\"]+)['\"]", re.I)
HEAD = re.compile(
    r"<h3[^>]*\bjobs__box__heading\b[^>]*>(.*?)</h3>", re.S | re.I
)
WHERE = re.compile(r"<p[^>]*\bjobs__box__text\b[^>]*>(.*?)</p>", re.S | re.I)
PAGES = re.compile(r"data-page=['\"](\d+)['\"]", re.I)
PAGE = 20
MAX_PAGES = 25
NOT_BOARDS = {"www", "api", "app", "support"}


class TalentLyft(SubdomainVendor):
    name = "talentlyft"
    label = "TalentLyft"
    example = "gp-krk"
    hint = "It is the subdomain: <slug>.talentlyft.com."
    public_url_tpl = "https://{board}.talentlyft.com"
    base_domains = ("talentlyft.com",)
    uses_previous = True

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in NOT_BOARDS else board

    @staticmethod
    def parse_fragment(html: str, base: str) -> list[tuple[str, str, str]]:
        out = []
        for box in BOX.findall(html):
            href = HREF.search(box)
            if not href:
                continue
            head, where = HEAD.search(box), WHERE.search(box)
            out.append(
                (
                    urljoin(base, unescape(href.group(1))),
                    text(head.group(1)) if head else "",
                    text(where.group(1)) if where else "",
                )
            )
        return out

    def fetch(self, board, previous=None):
        origin = f"https://{board}.talentlyft.com"
        rows, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            r = http.get(
                f"{origin}/JobList/",
                params={
                    "layoutId": "Jobs-1",
                    "websiteUrl": origin,
                    "themeId": "2",
                    "language": "en",
                    "subdomain": board,
                    "page": page,
                    "pageSize": PAGE,
                    "contains": "",
                },
                headers={
                    "x-requested-with": "XMLHttpRequest",
                    "Referer": origin + "/",
                },
            )
            if r.status_code in (404, 410):
                raise BoardGone(board)
            r.raise_for_status()
            batch = [
                x
                for x in self.parse_fragment(r.text, origin)
                if x[0] not in seen
            ]
            seen.update(x[0] for x in batch)
            rows += batch
            pages = max([int(n) for n in PAGES.findall(r.text)] or [1])
            if not batch or page >= pages:
                break
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
