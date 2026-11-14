"""CareerPlug: https://<slug>.careerplug.com/jobs lists the postings; each
posting page carries its text as JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, jobs_of, page_detail, text

ROW = re.compile(
    r"<a[^>]*\baria-label=[\"'][^\"']*[\"'][^>]*\bhref=[\"'](/jobs/\d+[^\"']*)"
    r"[\"'][^>]*>(.*?)</a>",
    re.S | re.I,
)


def _div(cls: str, body: str) -> str:
    m = re.search(rf"<div[^>]*\b{cls}\b[^>]*>(.*?)</div>", body, re.S | re.I)
    return text(m.group(1)) if m else ""


class CareerPlug(SubdomainVendor):
    name = "careerplug"
    label = "CareerPlug"
    example = "acme"
    hint = "It is the subdomain: <slug>.careerplug.com."
    public_url_tpl = "https://{board}.careerplug.com/jobs"
    base_domains = ("careerplug.com",)
    uses_previous = True

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        seen, out = set(), []
        for href, body in ROW.findall(html):
            url = urljoin(base, unescape(href))
            if url in seen:
                continue
            seen.add(url)
            where = re.sub(r"^\s*Location:\s*", "", _div("job-location", body))
            out.append((url, _div("job-title", body), where))
        return out

    def fetch(self, board, previous=None):
        base = f"https://{board}.careerplug.com/jobs"
        html = get_page(base, board)
        rows = self.parse_list(html, base)
        if not rows and "careerplug" not in html.lower():
            raise SoftBlocked(f"{board}: not a CareerPlug careers page")
        return enrich(jobs_of(self.name, board, rows), previous, page_detail)
