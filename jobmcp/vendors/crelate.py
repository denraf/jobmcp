"""Crelate: https://jobs.crelate.com/portal/<slug> names the organisation in
its page, and the candidate-portal API lists that organisation's postings
with their text."""

from __future__ import annotations

import json
import re

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SoftBlocked, Vendor, valid_slug

ORG = re.compile(r"var\s+ORG_ID\s*=\s*[\"']([0-9a-fA-F-]{8,})[\"']")
API = "https://jobs.crelate.com/api/candidateportal/GetAllJobs"


class Crelate(Vendor):
    name = "crelate"
    label = "Crelate"
    example = "acme"
    hint = "It is the last part of the URL: jobs.crelate.com/portal/<slug>."
    public_url_tpl = "https://jobs.crelate.com/portal/{board}"
    cdx_queries = (("jobs.crelate.com", "domain"),)

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "jobs.crelate.com":
            return None
        if len(segs) < 2 or segs[0] != "portal" or not valid_slug(segs[1]):
            return None
        return segs[1].lower()

    def fetch(self, board):
        base = f"https://jobs.crelate.com/portal/{board}"
        page = http.get(base)
        if page.status_code in (404, 410):
            raise BoardGone(board)
        page.raise_for_status()
        m = ORG.search(page.text)
        if not m:
            raise SoftBlocked(f"{board}: no organisation id in the page")
        envelope = {
            "Locations": None,
            "OrganizationId": m.group(1),
            "SearchText": None,
            "Tags": None,
        }
        r = http.get(API, params={"requestEnvelope": json.dumps(envelope)})
        r.raise_for_status()
        out = []
        for j in (r.json() or {}).get("Jobs") or []:
            rel = (j.get("Url") or "").strip()
            if not rel:
                continue
            url = (
                rel
                if rel.startswith("http")
                else f"{base}/job/{rel.lstrip('/')}"
            )
            where = ", ".join(
                str(j.get(k)).strip()
                for k in ("City", "State", "Country")
                if j.get(k)
            )
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("Title") or "").strip(),
                    company=board,
                    location=where,
                    url=url,
                    posted_at=j.get("LastPostedOnDate") or "",
                    description=clean_html(
                        j.get("Description") or j.get("JobDescription")
                    ),
                )
            )
        return out
