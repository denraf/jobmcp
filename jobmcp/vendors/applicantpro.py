"""ApplicantPro, and iSolved Hire which runs the same engine:
https://<slug>.applicantpro.com/jobs/ names the site's id in its page;
/core/jobs/<id> lists the postings, and each posting page carries its text as
JSON-LD."""

from __future__ import annotations

import re

from .. import http
from ..models import Job
from .base import BoardGone, SoftBlocked, SubdomainVendor
from .detail import enrich, get_page, page_detail

DOMAIN_ID = re.compile(
    r"[\"']?domain_id[\"']?\s*[:=]\s*[\"']?(\d{2,})[\"']?", re.I
)
NOT_BOARDS = {"www", "api", "admin", "app", "login"}


class _Engine(SubdomainVendor):
    uses_previous = True

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in NOT_BOARDS else board

    @staticmethod
    def _location(j: dict) -> str:
        where = j.get("jobLocation") or ", ".join(
            x for x in (j.get("city"), j.get("stateName")) if x
        )
        if (j.get("workplaceType") or "").lower() == "remote":
            where = f"Remote {where}".strip()
        return where

    def fetch(self, board, previous=None):
        origin = f"https://{board}.{self.base_domains[0]}"
        m = DOMAIN_ID.search(get_page(f"{origin}/jobs/", board))
        if not m:
            raise SoftBlocked(f"{board}: no site id in the page")
        r = http.get(
            f"{origin}/core/jobs/{m.group(1)}",
            params={"getParams": "{}"},
            headers={"Accept": "application/json"},
        )
        if r.status_code in (404, 410):
            raise BoardGone(board)
        r.raise_for_status()
        data = r.json() or {}
        if data.get("success") is False:
            raise SoftBlocked(f"{board}: {data.get('message') or 'refused'}")
        listed = []
        for j in (data.get("data") or {}).get("jobs") or []:
            url = j.get("jobUrl") or (
                f"{origin}/jobs/{j['id']}" if j.get("id") else ""
            )
            if url:
                listed.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(j.get("title") or "").strip(),
                        company=(j.get("orgTitle") or "").strip() or board,
                        location=self._location(j),
                        url=url,
                        posted_at=j.get("startDateRef") or "",
                    )
                )
        return enrich(listed, previous, page_detail)


class ApplicantPro(_Engine):
    name = "applicantpro"
    label = "ApplicantPro"
    example = "esncc"
    hint = "It is the subdomain: <slug>.applicantpro.com."
    public_url_tpl = "https://{board}.applicantpro.com/jobs/"
    base_domains = ("applicantpro.com",)


class ISolved(_Engine):
    name = "isolved"
    label = "iSolved Hire"
    example = "zaxbys"
    hint = "It is the subdomain: <slug>.isolvedhire.com."
    public_url_tpl = "https://{board}.isolvedhire.com/jobs/"
    base_domains = ("isolvedhire.com",)
