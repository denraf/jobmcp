from __future__ import annotations

from .. import http
from ..models import Job
from .base import BoardGone, SubdomainVendor


class BambooHR(SubdomainVendor):
    name = "bamboohr"
    label = "BambooHR"
    example = "acme"
    hint = "It is the subdomain: <slug>.bamboohr.com/careers."
    public_url_tpl = "https://{board}.bamboohr.com/careers"
    base_domains = ("bamboohr.com",)

    def fetch(self, board):
        r = http.get(
            f"https://{board}.bamboohr.com/careers/list",
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        if "json" not in r.headers.get("content-type", ""):
            raise BoardGone(
                board
            )  # unknown subdomains serve an HTML sign-in page
        out = []
        for j in r.json().get("result", []):
            loc = j.get("location") or {}
            where = ", ".join(
                x for x in (loc.get("city"), loc.get("state")) if x
            )
            if j.get("isRemote"):
                where = f"Remote - {where}" if where else "Remote"
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("jobOpeningName") or "").strip(),
                    company=board,
                    location=where,
                    url=f"https://{board}.bamboohr.com/careers/{j.get('id')}",
                )
            )
        return out
