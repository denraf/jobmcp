"""CareerPuck: api.careerpuck.com/v1/public/job-boards/<slug> lists the
postings with their text (HTML, entity-escaped)."""

from __future__ import annotations

from html import unescape

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, Vendor, valid_slug


class CareerPuck(Vendor):
    name = "careerpuck"
    label = "CareerPuck"
    example = "lyft"
    hint = (
        "It is the last part of the URL: app.careerpuck.com/job-board/<slug>."
    )
    public_url_tpl = "https://app.careerpuck.com/job-board/{board}"
    cdx_queries = (("app.careerpuck.com", "domain"),)

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host not in ("app.careerpuck.com", "www.app.careerpuck.com"):
            return None
        if len(segs) < 2 or segs[0] != "job-board" or not valid_slug(segs[1]):
            return None
        return segs[1].lower()

    def fetch(self, board):
        r = http.get(
            f"https://api.careerpuck.com/v1/public/job-boards/{board}"
        )
        if r.status_code == 404:
            raise BoardGone(board)
        r.raise_for_status()
        data = r.json() or {}
        company = (data.get("employerProfile") or {}).get("name") or board
        out = []
        for j in data.get("jobs") or []:
            if (j.get("status") or "public").lower() != "public":
                continue
            where = (j.get("location") or "").strip()
            if (j.get("workplaceType") or "").lower() == "remote":
                where = f"Remote {where}".strip()
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=company,
                    location=where,
                    url=j.get("publicUrl") or j.get("applyUrl") or "",
                    posted_at=j.get("postedAt") or "",
                    description=clean_html(unescape(j.get("content") or "")),
                )
            )
        return out
