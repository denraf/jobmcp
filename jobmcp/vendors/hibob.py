"""HiBob: https://<slug>.careers.hibob.com/api/job-ad lists the postings with
their text."""

from __future__ import annotations

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SubdomainVendor

SECTIONS = ("description", "responsibilities", "requirements", "benefits")


class HiBob(SubdomainVendor):
    name = "hibob"
    label = "HiBob"
    example = "acme"
    hint = "It is the subdomain: <slug>.careers.hibob.com."
    public_url_tpl = "https://{board}.careers.hibob.com"
    base_domains = ("careers.hibob.com",)

    def fetch(self, board):
        host = f"{board}.careers.hibob.com"
        r = http.get(
            f"https://{host}/api/job-ad",
            headers={
                "Accept": "application/json",
                "companyidentifier": board,
                "Origin": f"https://{host}",
                "Referer": f"https://{host}/",
            },
        )
        if r.status_code == 404:
            raise BoardGone(board)
        r.raise_for_status()
        out = []
        for j in r.json().get("jobAdDetails") or []:
            if not j.get("id"):
                continue
            labels = j.get("sectionLabels")
            labels = labels if isinstance(labels, dict) else {}
            parts = []
            for key in SECTIONS:
                body = clean_html(j.get(key))
                if body:
                    head = (labels.get(key) or "").strip()
                    parts.append(f"{head}\n{body}" if head else body)
            where = ", ".join(
                x for x in (j.get("site"), j.get("country")) if x
            )
            if (j.get("workspaceType") or "").lower() == "remote":
                where = f"Remote {where}".strip()
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=board,
                    location=where,
                    url=j.get("jobUrl")
                    or j.get("url")
                    or f"https://{host}/jobs/{j['id']}",
                    posted_at=j.get("publishedAt") or "",
                    description="\n\n".join(parts),
                )
            )
        return out
