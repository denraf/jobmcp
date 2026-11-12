from __future__ import annotations

from .. import http
from ..models import Job, clean_html
from .base import Vendor, valid_slug


class Workable(Vendor):
    name = "workable"
    label = "Workable"
    example = "huggingface"
    hint = (
        "It is the first part of the careers URL: apply.workable.com/<slug>."
    )
    public_url_tpl = "https://apply.workable.com/{board}"
    cdx_queries = (("apply.workable.com", "domain"),)

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "apply.workable.com" or not segs:
            return None
        # /api/v1/widget/accounts/{slug} or /{slug}/j/{shortcode}
        if segs[0] == "api":
            return (
                segs[4].lower()
                if len(segs) > 4
                and segs[1:4] == ["v1", "widget", "accounts"]
                and valid_slug(segs[4])
                else None
            )
        return segs[0].lower() if valid_slug(segs[0]) else None

    def fetch(self, board):
        r = http.get(
            f"https://apply.workable.com/api/v1/widget/accounts/{board}?details=true"
        )
        r.raise_for_status()
        data = r.json()
        company = data.get("name") or board
        out = []
        for j in data.get("jobs", []):
            loc = ", ".join(
                x
                for x in (j.get("city"), j.get("state"), j.get("country"))
                if x
            )
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=company,
                    location=loc,
                    url=j.get("url") or j.get("shortlink") or "",
                    posted_at=j.get("published_on")
                    or j.get("created_at")
                    or "",
                    description=clean_html(j.get("description")),
                )
            )
        return out
