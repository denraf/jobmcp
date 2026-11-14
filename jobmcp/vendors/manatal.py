"""Manatal career pages: www.careers-page.com/<slug> (or <slug>.careers-page
.com); the page's JSON API lists the postings with their text, 20 a page."""

from __future__ import annotations

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, Vendor, valid_slug

API = "https://www.careers-page.com/api/v1.0/c/{board}/jobs/"
MAX_PAGES = 60
NOT_BOARDS = {"www", "api", "job", "jobs", "static", "media", "login"}


class Manatal(Vendor):
    name = "manatal"
    label = "Manatal"
    example = "stonealliancegroup"
    hint = "It is the last part of the URL: www.careers-page.com/<slug>."
    public_url_tpl = "https://www.careers-page.com/{board}"
    cdx_queries = (("careers-page.com", "domain"),)

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host == "www.careers-page.com":
            slug = segs[0] if segs else ""
        elif host.endswith(".careers-page.com"):
            slug = host[: -len(".careers-page.com")]
        else:
            return None
        if slug.lower() in NOT_BOARDS or not valid_slug(slug):
            return None
        return slug.lower()

    def fetch(self, board):
        out, url, params = [], API.format(board=board), {"page_size": 50}
        for _ in range(MAX_PAGES):
            r = http.get(url, params=params)
            if r.status_code in (404, 410):
                raise BoardGone(board)
            r.raise_for_status()
            data = r.json() or {}
            for j in data.get("results") or []:
                if not j.get("hash"):
                    continue
                where = j.get("location_display") or ", ".join(
                    x
                    for x in (j.get("city"), j.get("state"), j.get("country"))
                    if x
                )
                if j.get("is_remote"):
                    where = f"Remote {where}".strip()
                out.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(j.get("position_name") or "").strip(),
                        company=(j.get("company_name") or "").strip() or board,
                        location=where,
                        url=f"https://www.careers-page.com/{board}/job/"
                        f"{j['hash']}",
                        posted_at=j.get("created_at")
                        or j.get("published_at")
                        or "",
                        description=clean_html(j.get("description")),
                    )
                )
            url, params = data.get("next"), None
            if not url:
                break
        return out
