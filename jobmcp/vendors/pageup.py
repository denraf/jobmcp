"""PageUp: https://careers.pageuppeople.com/<id>/<site>/<lang>/listing/ lists
the postings in HTML; a posting's page holds its text in #job-details.

Board key: '<id>/<site>', e.g. '994/cw'.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SoftBlocked, Vendor
from .detail import div_inner, enrich, text

LINK = re.compile(
    r"<a[^>]*\bjob-link\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I
)
WHERE = re.compile(r"<span[^>]*\blocation\b[^>]*>(.*?)</span>", re.S | re.I)
BODY = re.compile(r"id=\"job-details\"")
KEY = re.compile(r"^\d+/[A-Za-z0-9_-]+$")
MAX_PAGES = 30


class PageUp(Vendor):
    name = "pageup"
    uses_previous = True
    label = "PageUp"
    example = "994/cw"
    hint = (
        "It is the two parts after the host of the careers URL: "
        "careers.pageuppeople.com/<id>/<site>/en-us/listing -> <id>/<site>."
    )
    public_url_tpl = "https://careers.pageuppeople.com/{board}/en-us/listing/"
    cdx_queries = (("careers.pageuppeople.com", "domain"),)

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/")
        m = re.search(r"pageuppeople\.com/(\d+)/([A-Za-z0-9_-]+)", s)
        if m:
            s = f"{m.group(1)}/{m.group(2).lower()}"
        if not KEY.match(s):
            raise ValueError(
                f"{slug!r} is not a valid PageUp board name. {self.hint}"
            )
        a, b = s.split("/")
        return f"{a}/{b.lower()}"

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "careers.pageuppeople.com" or not segs:
            return None
        # a bare /<id> link lands on the default site, "cw"
        site = segs[1].lower() if len(segs) > 1 else "cw"
        key = f"{segs[0]}/{site}"
        return key if KEY.match(key) else None

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

    @staticmethod
    def _detail(url: str) -> dict:
        try:
            r = http.get(url, retries=2)
            if r.status_code != 200:
                return {}
            body = clean_html(div_inner(r.text, BODY))
            return {"description": body} if body else {}
        except Exception:  # one posting page failing is not a failed board
            return {}

    def fetch(self, board, previous=None):
        base = f"https://careers.pageuppeople.com/{board}/en-us/listing/"
        rows, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            r = http.get(base, params={"page": page} if page > 1 else None)
            if r.status_code in (404, 410) and page == 1:  # no en-us site
                base = base.replace("/en-us/", "/en/")
                r = http.get(base)
            if r.status_code in (404, 410):
                raise BoardGone(board)
            r.raise_for_status()
            base = r.url.split("?")[0]  # the site may have redirected
            batch = [
                x for x in self.parse_list(r.text, base) if x[0] not in seen
            ]
            if page == 1 and not batch and "pageup" not in r.text.lower():
                raise SoftBlocked(f"{board}: not a PageUp careers page")
            if not batch:
                break
            seen.update(x[0] for x in batch)
            rows += batch
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
        return enrich(listed, previous, self._detail)
