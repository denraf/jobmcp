"""Recruiterflow: https://recruiterflow.com/<slug>/jobs embeds its postings as
`window.jobsList = {...}`; each posting page carries its text as JSON-LD."""

from __future__ import annotations

import json
import re

from ..models import Job
from .base import SoftBlocked, Vendor, valid_slug
from .detail import enrich, get_page, page_detail

MARK = re.compile(r"window\.jobsList\s*=\s*")
HOSTS = ("recruiterflow.com", "www.recruiterflow.com")
NOT_BOARDS = {"jobs", "api", "static", "login", "signup", "blog", "pricing"}


class Recruiterflow(Vendor):
    name = "recruiterflow"
    label = "Recruiterflow"
    example = "jenix"
    hint = "It is the first part of the careers URL: recruiterflow.com/<slug>."
    public_url_tpl = "https://recruiterflow.com/{board}/jobs"
    cdx_queries = (("recruiterflow.com", "domain"),)
    uses_previous = True

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host not in HOSTS or not segs:
            return None
        if segs[0].lower() in NOT_BOARDS or not valid_slug(segs[0]):
            return None
        return segs[0].lower()

    @staticmethod
    def jobs_list(html: str) -> dict | None:
        m = MARK.search(html)
        if not m:
            return None
        try:
            obj, _ = json.JSONDecoder().raw_decode(html, m.end())
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    def fetch(self, board, previous=None):
        html = get_page(f"https://recruiterflow.com/{board}/jobs", board)
        data = self.jobs_list(html)
        if data is None:
            raise SoftBlocked(f"{board}: no job list in the page")
        listed = []
        for group in data.get("department") or []:
            if not isinstance(group, list) or len(group) < 2:
                continue
            for j in group[1] or []:
                link = (j.get("apply_link") or "").strip("/")
                if not link:
                    continue
                listed.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(j.get("job_name") or "").strip(),
                        company=board,
                        location=(j.get("details") or "").strip(),
                        url=f"https://recruiterflow.com/{link}",
                        posted_at=j.get("last_opened") or "",
                    )
                )
        return enrich(listed, previous, page_detail)
