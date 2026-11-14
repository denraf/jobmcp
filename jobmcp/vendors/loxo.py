"""Loxo: https://app.loxo.co/<slug> lists the postings in HTML; a posting's
page holds its text in the description block."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from ..models import Job, clean_html
from .base import SoftBlocked, Vendor, valid_slug
from .detail import div_inner, enrich, get_page, text

LINK = re.compile(
    r"<a[^>]*\bjob-title\b[^>]*href=\"(/job/[^\"]+)\"[^>]*>(.*?)</a>"
    r"|<a[^>]*href=\"(/job/[^\"]+)\"[^>]*\bjob-title\b[^>]*>(.*?)</a>",
    re.S | re.I,
)
WHERE = re.compile(r"<div[^>]*\bjob-location\b[^>]*>(.*?)</div>", re.S | re.I)
BODY = re.compile(r"class='cleanslate'|class=\"cleanslate\"")
NOT_BOARDS = {"job", "jobs", "api", "login", "static"}


class Loxo(Vendor):
    name = "loxo"
    label = "Loxo"
    example = "Brave-New-World"
    hint = "It is the last part of the URL: app.loxo.co/<slug>."
    public_url_tpl = "https://app.loxo.co/{board}"
    cdx_queries = (("app.loxo.co", "domain"),)
    case_sensitive = True
    uses_previous = True

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "app.loxo.co" or not segs:
            return None
        if segs[0].lower() in NOT_BOARDS or not valid_slug(segs[0]):
            return None
        return segs[0]

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        found = list(LINK.finditer(html))
        out, seen = [], set()
        for m, nxt in zip(found, found[1:] + [None]):
            href, title = (
                (m.group(1), m.group(2))
                if m.group(1)
                else (
                    m.group(3),
                    m.group(4),
                )
            )
            url = urljoin(base, unescape(href))
            if url in seen:
                continue
            seen.add(url)
            block = html[m.end() : nxt.start() if nxt else len(html)]
            where = WHERE.search(block)
            place = (
                re.sub(r"^\s*location_on\s*", "", text(where.group(1)))
                if where
                else ""
            )
            out.append((url, text(title), place))
        return out

    @staticmethod
    def _detail(url: str) -> dict:
        from .. import http

        try:
            r = http.get(url, retries=2)
            if r.status_code != 200:
                return {}
            body = clean_html(div_inner(r.text, BODY))
            return {"description": body} if body else {}
        except Exception:  # one posting page failing is not a failed board
            return {}

    def fetch(self, board, previous=None):
        base = f"https://app.loxo.co/{board}"
        html = get_page(base, board)
        rows = self.parse_list(html, "https://app.loxo.co/")
        if not rows and "loxo" not in html.lower():
            raise SoftBlocked(f"{board}: not a Loxo careers page")
        listed = [
            Job(
                vendor=self.name,
                board=board,
                title=t,
                company=board.replace("-", " "),
                location=loc,
                url=u,
            )
            for u, t, loc in rows
        ]
        return enrich(listed, previous, self._detail)
