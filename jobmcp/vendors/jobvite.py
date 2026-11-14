"""Jobvite: https://jobs.jobvite.com/<slug>/jobs lists a company's postings
in HTML tables; each posting page carries its text as JSON-LD."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from .. import http
from ..models import Job
from .base import BoardGone, SoftBlocked, Vendor, valid_slug
from .detail import enrich, page_detail

ROW = re.compile(
    r"<tr[^>]*>.*?<td[^>]*\bjv-job-list-name\b[^>]*>.*?"
    r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>.*?</td>.*?"
    r"<td[^>]*\bjv-job-list-location\b[^>]*>(.*?)</td>.*?</tr>",
    re.S | re.I,
)
TAGS = re.compile(r"<[^>]+>")
NOT_BOARDS = {"job", "jobs", "search", "careers", "api", "static"}


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAGS.sub(" ", s))).strip()


class Jobvite(Vendor):
    name = "jobvite"
    label = "Jobvite"
    example = "cetera"
    hint = "It is the first part of the careers URL: jobs.jobvite.com/<slug>."
    public_url_tpl = "https://jobs.jobvite.com/{board}/jobs"
    cdx_queries = (("jobs.jobvite.com", "domain"),)
    uses_previous = True

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host not in ("jobs.jobvite.com", "careers.jobvite.com"):
            return None
        if (
            not segs
            or segs[0].lower() in NOT_BOARDS
            or not valid_slug(segs[0])
        ):
            return None
        return segs[0].lower()

    @staticmethod
    def parse_list(html: str, base: str) -> list[tuple[str, str, str]]:
        """[(url, title, location)]"""
        seen, out = set(), []
        for href, title, loc in ROW.findall(html):
            url = urljoin(base, unescape(href.strip()))
            if url not in seen:
                seen.add(url)
                out.append((url, _text(title), _text(loc)))
        return out

    def fetch(self, board, previous=None):
        base = f"https://jobs.jobvite.com/{board}/jobs"
        r = http.get(base)
        if r.status_code == 404:
            raise BoardGone(board)
        r.raise_for_status()
        rows = self.parse_list(r.text, base)
        if not rows and "jv-" not in r.text:
            raise SoftBlocked(f"{board}: not a Jobvite careers page")
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
        return enrich(listed, previous, page_detail)
