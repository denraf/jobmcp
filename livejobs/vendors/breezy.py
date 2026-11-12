from __future__ import annotations

from .. import http
from ..models import Job
from .base import SubdomainVendor


class Breezy(SubdomainVendor):
    name = "breezy"
    label = "Breezy HR"
    example = "acme"
    hint = "It is the subdomain: <slug>.breezy.hr."
    public_url_tpl = "https://{board}.breezy.hr"
    base_domains = ("breezy.hr",)

    def fetch(self, board):
        r = http.get(f"https://{board}.breezy.hr/json")
        r.raise_for_status()
        out = []
        for j in r.json():
            loc = j.get("location") or {}
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("name") or "").strip(),
                    company=(j.get("company") or {}).get("name") or board,
                    location=loc.get("name") or "",
                    url=j.get("url") or "",
                    posted_at=j.get("published_date") or "",
                )
            )
        return out
