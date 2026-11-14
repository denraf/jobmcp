"""Recruit CRM: https://recruitcrm.io/jobs/<account> is served by a POST
API that lists the account's jobs with their text.

Board key: the account name (upper and lower case matter).
"""

from __future__ import annotations

import re

from .. import http
from ..models import Job, clean_html
from .base import Vendor, valid_slug

API = "https://albatross.recruitcrm.io/v1/external-pages/jobs-by-account/get"
PAGE = 100
MAX_PAGES = 30


class RecruitCRM(Vendor):
    name = "recruitcrm"
    label = "Recruit CRM"
    example = "Talentbank_1_jobs"
    hint = "It is the last part of the URL: recruitcrm.io/jobs/<account>."
    public_url_tpl = "https://recruitcrm.io/jobs/{board}"
    cdx_queries = (("recruitcrm.io", "domain"),)
    case_sensitive = True

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/")
        m = re.search(r"recruitcrm\.io/jobs/([^/?#]+)", s)
        s = m.group(1) if m else s
        if not valid_slug(s):
            raise ValueError(
                f"{slug!r} is not a valid Recruit CRM board name. {self.hint}"
            )
        return s

    def board_from_url(self, url):
        host, segs, query = self.split(url)
        if host != "recruitcrm.io" and not host.endswith(".recruitcrm.io"):
            return None
        if len(segs) >= 2 and segs[0] == "jobs" and valid_slug(segs[1]):
            return segs[1]
        m = re.search(r"(?:^|&)account=([^&]+)", query)
        return m.group(1) if m and valid_slug(m.group(1)) else None

    def fetch(self, board):
        out, seen = [], set()
        for page in range(MAX_PAGES):
            r = http.post(
                API,
                params={"account": board, "batch": "true"},
                json={
                    "limit": PAGE,
                    "offset": page * PAGE,
                    "search_data": "",
                    "onlyJobs": True,
                },
                headers={
                    "Origin": "https://recruitcrm.io",
                    "Referer": f"https://recruitcrm.io/jobs/{board}",
                },
            )
            r.raise_for_status()
            jobs = ((r.json() or {}).get("data") or {}).get("jobs") or []
            fresh = [j for j in jobs if j.get("slug") not in seen]
            for j in fresh:
                seen.add(j["slug"])
                where = (
                    "Remote"
                    if str(j.get("remote") or "") == "1"
                    else ", ".join(
                        x
                        for x in (j.get("city"), j.get("locality"))
                        if x and x != "None"
                    )
                )
                out.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(j.get("name") or "").strip(),
                        company=(j.get("companyname") or "").strip() or board,
                        location=where,
                        url=f"https://recruitcrm.io/jobs/{board}/{j['slug']}",
                        posted_at=j.get("posted_at")
                        or j.get("createdon")
                        or "",
                        description=clean_html(j.get("jdtext")),
                    )
                )
            if not fresh or len(jobs) < PAGE:
                break
        return out
