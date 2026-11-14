"""Occupop: the careers site at https://<key>.occupop-careers.com is served
from a GraphQL API that lists a company's live jobs with their text."""

from __future__ import annotations

from .. import http
from ..models import Job, clean_html
from .base import SubdomainVendor

API = "https://gateway.server.occupop.com/graphql"
QUERY = (
    "query LiveJobs($companyKey: String!, $tags: [String!], "
    "$includeAllBrandsJobs: Boolean) { careersPage { liveJobs("
    "companyKey: $companyKey tags: $tags "
    "includeAllBrandsJobs: $includeAllBrandsJobs) { uuid title "
    "publishedAt companyName description location { city country } "
    "period } } }"
)


class Occupop(SubdomainVendor):
    name = "occupop"
    label = "Occupop"
    example = "bowdistribution"
    hint = "It is the subdomain: <key>.occupop-careers.com."
    public_url_tpl = "https://{board}.occupop-careers.com"
    base_domains = ("occupop-careers.com",)

    def board_from_url(self, url):
        board = super().board_from_url(url)
        # a copied "contextmenu-<key>" host is the same site as <key>
        return board.removeprefix("contextmenu-") if board else None

    def fetch(self, board):
        r = http.post(
            API,
            json={
                "operationName": "LiveJobs",
                "variables": {"companyKey": board, "tags": []},
                "query": QUERY,
            },
            headers={
                "Origin": "https://www.occupop.com",
                "Referer": "https://www.occupop.com/",
            },
        )
        r.raise_for_status()
        jobs = (
            ((r.json() or {}).get("data") or {}).get("careersPage") or {}
        ).get("liveJobs") or []
        out = []
        for j in jobs:
            if not j.get("uuid"):
                continue
            loc = j.get("location") or {}
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=(j.get("companyName") or "").strip() or board,
                    location=", ".join(
                        x for x in (loc.get("city"), loc.get("country")) if x
                    ),
                    url=f"https://{board}.occupop-careers.com/job/{j['uuid']}",
                    posted_at=j.get("publishedAt") or "",
                    description=clean_html(j.get("description")),
                )
            )
        return out
