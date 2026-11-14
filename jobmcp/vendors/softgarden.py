"""Softgarden: https://<slug>.softgarden.io/vacancies lists the postings;
each posting page carries its text as JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, jobs_of, page_detail, text

BLOCK = re.compile(
    r"<div[^>]*class=\"[^\"]*\bmatchElement\b[^\"]*\"[^>]*id=\"job_id_\d+\"",
    re.I,
)
LINK = re.compile(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
PLACE = re.compile(
    r"<span[^>]*\blocation-view-item\b[^>]*>(.*?)</span>", re.S | re.I
)
CITY = re.compile(
    r"<div class=\"matchValue ProjectGeoLocationCity\">(.*?)</div>",
    re.S | re.I,
)


class Softgarden(SubdomainVendor):
    name = "softgarden"
    label = "Softgarden"
    example = "acme"
    hint = "It is the subdomain: <slug>.softgarden.io."
    public_url_tpl = "https://{board}.softgarden.io/vacancies"
    base_domains = ("softgarden.io",)
    uses_previous = True

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        starts = [m.start() for m in BLOCK.finditer(html)]
        out, seen = [], set()
        for a, b in zip(starts, starts[1:] + [len(html)]):
            block = html[a:b]
            link = LINK.search(block)
            if not link:
                continue
            url = urljoin(base, unescape(link.group(1)))
            if url in seen:
                continue
            seen.add(url)
            places = [
                text(p).rstrip(",").strip() for p in PLACE.findall(block)
            ]
            city = CITY.search(block)
            where = ", ".join(p for p in places if p) or (
                text(city.group(1)) if city else ""
            )
            out.append((url, text(link.group(2)), where))
        return out

    def fetch(self, board, previous=None):
        base = f"https://{board}.softgarden.io/vacancies"
        html = get_page(base, board)
        rows = self.parse_list(html, base)
        if not rows and "softgarden" not in html.lower():
            raise SoftBlocked(f"{board}: not a Softgarden careers page")
        return enrich(jobs_of(self.name, board, rows), previous, page_detail)
