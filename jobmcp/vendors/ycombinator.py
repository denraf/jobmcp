"""Y Combinator's Work at a Startup: https://www.ycombinator.com/companies/
<slug>/jobs embeds a company's openings as JSON in the page; each opening's
page carries its text as JSON-LD."""

from __future__ import annotations

import json
import re
from html import unescape
from functools import partial
from urllib.parse import urljoin

from .. import config, http
from ..models import Job
from .base import SoftBlocked, Vendor, valid_slug
from .detail import enrich, get_page, page_detail

PAGE = re.compile(
    r"<div[^>]*id=\"WaasShowJobsPage-react-component-[^\"]+\"[^>]*"
    r"data-page=\"([^\"]*)\"",
    re.S,
)
HOSTS = ("www.ycombinator.com", "ycombinator.com")
DETAIL_PER_RUN = 60  # at one page every couple of seconds, a couple of minutes

# One gate for the whole host: the list pages and the posting pages of every
# company are spaced out together, and when the site answers 429 all the
# threads pause together (honoring Retry-After) instead of each retrying on
# its own timer.
GATE = http.Gate(
    "ycombinator.com",
    min_interval=config.YC_MIN_INTERVAL,
    max_cooldown=120.0,
    trip_after=8,  # blocked, not busy: stop and retry next sync
)


class YCombinator(Vendor):
    name = "ycombinator"
    label = "Y Combinator"
    example = "ambient-ai"
    hint = (
        "It is the company part of its jobs page: "
        "ycombinator.com/companies/<slug>/jobs."
    )
    public_url_tpl = "https://www.ycombinator.com/companies/{board}/jobs"
    cdx_queries = (("www.ycombinator.com/companies/", "prefix"),)
    uses_previous = True

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host not in HOSTS or len(segs) < 3:
            return None
        if segs[0] != "companies" or segs[2] != "jobs":
            return None
        return segs[1].lower() if valid_slug(segs[1]) else None

    @staticmethod
    def parse_list(html: str) -> tuple[str, list[dict]] | None:
        m = PAGE.search(html)
        if not m:
            return None
        try:
            props = json.loads(unescape(m.group(1))).get("props") or {}
        except ValueError:
            return None
        name = (props.get("company") or {}).get("name") or ""
        return name, props.get("jobPostings") or []

    def fetch(self, board, previous=None):
        base = f"https://www.ycombinator.com/companies/{board}/jobs"
        parsed = self.parse_list(get_page(base, board, gate=GATE))
        if parsed is None:
            raise SoftBlocked(f"{board}: no openings data in the page")
        company, items = parsed
        listed = []
        for j in items:
            url = urljoin(base, j.get("url") or j.get("applyUrl") or "")
            if not url:
                continue
            listed.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=company or j.get("companyName") or board,
                    location=(j.get("location") or "").strip(),
                    url=url,
                )
            )
        return enrich(
            listed,
            previous,
            partial(page_detail, gate=GATE),
            per_run=DETAIL_PER_RUN,
        )
