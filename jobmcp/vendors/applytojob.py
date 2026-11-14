"""ApplyToJob (JazzHR): https://<slug>.applytojob.com/apply lists the
postings; each posting page carries its text as JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SoftBlocked, SubdomainVendor
from .detail import enrich, json_ld_posting, ld_fields

ITEM = re.compile(
    r"<li[^>]*class=[\"'][^\"']*\blist-group-item\b[^\"']*[\"'][^>]*>"
    r"(.*?)</li>",
    re.S | re.I,
)
HEAD = re.compile(
    r"<h3[^>]*list-group-item-heading[^>]*>.*?<a[^>]*href=[\"']([^\"']+)"
    r"[\"'][^>]*>(.*?)</a>",
    re.S | re.I,
)
WHERE = re.compile(r"fa-map-marker[^>]*></i>\s*([^<]+)", re.I)
LEGACY = re.compile(
    r"<a(?=[^>]*\bresumator-job-title-link\b)(?=[^>]*href=[\"']([^\"']+))"
    r"[^>]*>(.*?)</a>",
    re.S | re.I,
)
TAGS = re.compile(r"<[^>]+>")
BODY = re.compile(
    r"id=[\"']job-description[\"'][^>]*>(.*?)"
    r"(?:<div[^>]*resumator-mobile-apply-wrapper|<div[^>]*id=[\"']job-appl)",
    re.S | re.I,
)


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAGS.sub(" ", s))).strip()


class ApplyToJob(SubdomainVendor):
    name = "applytojob"
    label = "ApplyToJob (JazzHR)"
    example = "jki"
    hint = "It is the subdomain: <slug>.applytojob.com."
    public_url_tpl = "https://{board}.applytojob.com/apply"
    base_domains = ("applytojob.com",)
    uses_previous = True

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        seen, out = set(), []
        for item in ITEM.findall(html):
            h = HEAD.search(item)
            if not h:
                continue
            url = urljoin(base, unescape(h.group(1).strip()))
            if url in seen:
                continue
            seen.add(url)
            w = WHERE.search(item)
            out.append(
                (url, _text(h.group(2)), _text(w.group(1)) if w else "")
            )
        for href, title in LEGACY.findall(html):  # the older template
            url = urljoin(base, unescape(href.strip()))
            if url not in seen:
                seen.add(url)
                out.append((url, _text(title), ""))
        return out

    @staticmethod
    def parse_posting(html: str) -> dict:
        """The text of a posting page (a JSON-LD JobPosting when the page has
        one, otherwise the description block)."""
        it = json_ld_posting(html)
        out = ld_fields(it) if it else {}
        if not out.get("description"):
            m = BODY.search(html)
            if m:
                out["description"] = clean_html(m.group(1))
        return {k: v for k, v in out.items() if v}

    def _detail(self, url: str) -> dict:
        try:
            r = http.get(url, retries=2)
            return self.parse_posting(r.text) if r.status_code == 200 else {}
        except Exception:  # one posting page failing is not a failed board
            return {}

    def fetch(self, board, previous=None):
        base = f"https://{board}.applytojob.com/apply"
        r = http.get(base)
        if r.status_code == 404:
            raise BoardGone(board)
        r.raise_for_status()
        rows = self.parse_list(r.text, base)
        if not rows and "applytojob" not in r.text.lower():
            raise SoftBlocked(f"{board}: not a JazzHR careers page")
        listed = [
            Job(
                vendor=self.name,
                board=board,
                title=title,
                company=board,
                location=loc,
                url=url,
            )
            for url, title, loc in rows
        ]
        return enrich(listed, previous, self._detail)
