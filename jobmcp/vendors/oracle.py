"""Oracle Recruiting Cloud: the candidate-experience site of a tenant at
https://<tenant>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/<lang>/
sites/<site>/jobs is served by a public REST API: one call lists the postings
(25 a page) and one call per posting returns its text.

Board key: '<tenant>.fa.<region>/<site>', e.g. 'ccyj.fa.us6/CX'.
"""

from __future__ import annotations

import re

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SoftBlocked, Vendor
from .detail import enrich

API = "hcmRestApi/resources/latest"
PAGE = 25
MAX_PAGES = 80
HOST = re.compile(r"^([a-z0-9-]+\.fa\.[a-z0-9-]+)\.oraclecloud\.com$")
KEY = re.compile(r"^[a-z0-9-]+\.fa\.[a-z0-9-]+/[A-Za-z0-9_.-]+$")


class Oracle(Vendor):
    name = "oracle"
    label = "Oracle Recruiting Cloud"
    example = "ccyj.fa.us6/CX"
    hint = (
        "It is the host (without .oraclecloud.com) and the site of the "
        "careers URL: <tenant>.fa.<region>.oraclecloud.com/hcmUI/"
        "CandidateExperience/en/sites/<site>/jobs -> <tenant>.fa.<region>/"
        "<site>."
    )
    public_url_tpl = ""
    cdx_queries = (("fa.oraclecloud.com", "domain"),)
    case_sensitive = True
    uses_previous = True

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/")
        m = re.search(
            r"//([a-z0-9-]+\.fa\.[a-z0-9-]+)\.oraclecloud\.com/.*?/sites/"
            r"([^/?#]+)",
            s,
            re.I,
        )
        if m:
            s = f"{m.group(1).lower()}/{m.group(2)}"
        if not KEY.match(s):
            raise ValueError(
                f"{slug!r} is not a valid Oracle board name. {self.hint}"
            )
        host, site = s.split("/")
        return f"{host.lower()}/{site}"

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        m = HOST.match(host)
        if not m or "sites" not in segs:
            return None
        i = segs.index("sites")
        if i + 1 >= len(segs):
            return None
        return f"{m.group(1)}/{segs[i + 1]}"

    def public_url(self, board):
        host, site = board.split("/")
        return (
            f"https://{host}.oraclecloud.com/hcmUI/CandidateExperience/en/"
            f"sites/{site}/jobs"
        )

    def fetch(self, board, previous=None):
        host, site = board.split("/")
        origin = f"https://{host}.oraclecloud.com"
        listed = []
        for page in range(MAX_PAGES):
            r = http.get(
                f"{origin}/{API}/recruitingCEJobRequisitions",
                params={
                    "onlyData": "true",
                    "expand": "requisitionList.workLocation",
                    "finder": f"findReqs;siteNumber={site},"
                    f"limit={PAGE},offset={page * PAGE},"
                    "sortBy=POSTING_DATES_DESC",
                },
            )
            if r.status_code in (404, 410):
                raise BoardGone(board)
            r.raise_for_status()
            items = (r.json() or {}).get("items") or []
            if not items:
                raise SoftBlocked(f"{board}: no result set in the answer")
            reqs = items[0].get("requisitionList") or []
            for q in reqs:
                if not q.get("Id"):
                    continue
                where = q.get("PrimaryLocation") or ""
                if (q.get("WorkplaceTypeCode") or "").upper() == "ORA_REMOTE":
                    where = f"Remote {where}".strip()
                listed.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(q.get("Title") or "").strip(),
                        company=host.split(".")[0],
                        location=where,
                        url=f"{origin}/hcmUI/CandidateExperience/en/sites/"
                        f"{site}/job/{q['Id']}",
                        posted_at=q.get("PostedDate") or "",
                    )
                )
            total = items[0].get("TotalJobsCount") or 0
            if len(reqs) < PAGE or (page + 1) * PAGE >= total:
                break

        def detail(url: str) -> dict:
            job_id = url.rsplit("/", 1)[-1]
            try:
                d = http.get(
                    f"{origin}/{API}/recruitingCEJobRequisitionDetails",
                    params={
                        "expand": "all",
                        "onlyData": "true",
                        "finder": f'ById;Id="{job_id}",siteNumber={site}',
                    },
                    retries=2,
                )
                if d.status_code != 200:
                    return {}
                it = ((d.json() or {}).get("items") or [{}])[0]
                parts = [
                    clean_html(it.get(k))
                    for k in (
                        "ExternalDescriptionStr",
                        "ExternalResponsibilitiesStr",
                        "ExternalQualificationsStr",
                    )
                ]
                body = "\n\n".join(p for p in parts if p)
                return {
                    "description": body,
                    "posted_at": it.get("ExternalPostedStartDate") or "",
                }
            except Exception:  # one posting failing is not a failed board
                return {}

        return enrich(listed, previous, detail)
