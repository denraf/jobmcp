"""Zoho Recruit: https://<slug>.zohorecruit.com/jobs/Careers lists the
postings as JSON in a hidden input of the page; a posting's own page carries
the rest of its text."""

from __future__ import annotations

import json
import re
from html import unescape

from ..models import Job, clean_html
from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, page_detail

JOBS = re.compile(r"<input[^>]*\bid=[\"']jobs[\"'][^>]*>", re.S | re.I)
VALUE = re.compile(r"\bvalue=\"([^\"]*)\"|\bvalue='([^']*)'", re.S)


class Zoho(SubdomainVendor):
    name = "zoho"
    label = "Zoho Recruit"
    example = "acme"
    hint = "It is the subdomain: <slug>.zohorecruit.com."
    public_url_tpl = "https://{board}.zohorecruit.com/jobs/Careers"
    base_domains = ("zohorecruit.com",)
    uses_previous = True

    @staticmethod
    def parse_list(html: str) -> list[dict] | None:
        tag = JOBS.search(html)
        v = VALUE.search(tag.group(0)) if tag else None
        if not v:
            return None
        try:
            data = json.loads(unescape(v.group(1) or v.group(2) or ""))
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    def fetch(self, board, previous=None):
        base = f"https://{board}.zohorecruit.com/jobs/Careers"
        items = self.parse_list(get_page(base, board))
        if items is None:
            raise SoftBlocked(f"{board}: no job list in the page")
        listed = []
        for j in items:
            if j.get("Publish") in (False, "False", "false") or not j.get(
                "id"
            ):
                continue
            where = ", ".join(
                str(j[k]).strip()
                for k in ("City", "State", "Country")
                if j.get(k)
            )
            listed.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(
                        j.get("Posting_Title")
                        or j.get("Job_Opening_Name")
                        or ""
                    ).strip(),
                    company=board,
                    location=where,
                    url=f"{base}/{j['id']}",
                    posted_at=str(j.get("Date_Opened") or ""),
                    description=clean_html(j.get("Job_Description")),
                )
            )
        return enrich(listed, previous, page_detail)
