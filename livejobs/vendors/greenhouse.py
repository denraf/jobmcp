from __future__ import annotations

from urllib.parse import parse_qs

from .. import http
from ..models import Job, clean_html
from .base import Vendor, valid_slug


class Greenhouse(Vendor):
    name = "greenhouse"
    label = "Greenhouse"
    example = "stripe"
    hint = (
        "It is the first part of the careers URL: boards.greenhouse.io/<slug>."
    )
    public_url_tpl = "https://boards.greenhouse.io/{board}"
    cdx_queries = (
        ("boards.greenhouse.io", "domain"),
        ("job-boards.greenhouse.io", "domain"),
        ("job-boards.eu.greenhouse.io", "domain"),
    )

    def board_from_url(self, url):
        host, segs, query = self.split(url)
        if not host.endswith("greenhouse.io"):
            return None
        if host.startswith("boards-api"):
            # /v1/boards/{slug}/jobs
            return (
                segs[2]
                if len(segs) > 2
                and segs[:2] == ["v1", "boards"]
                and valid_slug(segs[2])
                else None
            )
        if segs and segs[0] == "embed":
            # /embed/job_board?for={slug}  or  /embed/job_app?for={slug}
            slug = (parse_qs(query).get("for") or [""])[0]
            return slug.lower() if valid_slug(slug) else None
        if segs and valid_slug(segs[0]):
            return segs[0].lower()
        return None

    def fetch(self, board):
        # content=true so descriptions arrive.
        r = http.get(
            f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        )
        r.raise_for_status()
        out = []
        for j in r.json().get("jobs", []):
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=j.get("company_name") or board,
                    location=(j.get("location") or {}).get("name") or "",
                    url=j.get("absolute_url") or "",
                    posted_at=j.get("first_published")
                    or j.get("updated_at")
                    or "",
                    description=clean_html(j.get("content")),
                )
            )
        return out
