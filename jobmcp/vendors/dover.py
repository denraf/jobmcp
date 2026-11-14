"""Dover: app.dover.com/api/v1/careers-page-slug/<slug> names the company and
.../job-groups/<id>/job-groups lists its postings. Dover keeps the text of a
posting behind a login, so postings here have a title and a place only."""

from __future__ import annotations

from .. import http
from ..models import Job
from .base import BoardGone, Vendor, valid_slug

API = "https://app.dover.com/api/v1"


class Dover(Vendor):
    name = "dover"
    label = "Dover"
    example = "laylo"
    hint = "It is the last part of the URL: app.dover.com/jobs/<slug>."
    public_url_tpl = "https://app.dover.com/jobs/{board}"
    cdx_queries = (("app.dover.com", "domain"),)

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "app.dover.com" or len(segs) < 2:
            return None
        if segs[0] not in ("jobs", "apply") or not valid_slug(segs[1]):
            return None
        return segs[1].lower()

    @staticmethod
    def _location(job: dict) -> str:
        names = []
        for loc in job.get("locations") or []:
            name = loc.get("name") or (loc.get("location_option") or {}).get(
                "display_name"
            )
            if name and name not in names:
                names.append(name)
        where = " / ".join(names)
        if (job.get("workplace_type") or "").upper() == "REMOTE":
            where = f"Remote {where}".strip()
        return where

    def fetch(self, board):
        r = http.get(f"{API}/careers-page-slug/{board}")
        if r.status_code in (404, 410):
            raise BoardGone(board)
        r.raise_for_status()
        client = r.json() or {}
        if not client.get("id"):
            raise BoardGone(board)
        g = http.get(f"{API}/job-groups/{client['id']}/job-groups")
        g.raise_for_status()
        out, seen = [], set()
        for group in g.json() or []:
            for j in group.get("jobs") or []:
                if not j.get("id") or j["id"] in seen:
                    continue
                if j.get("is_published") is False:
                    continue
                seen.add(j["id"])
                out.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(j.get("title") or "").strip(),
                        company=client.get("name") or board,
                        location=self._location(j),
                        url=f"https://app.dover.com/apply/{board}/{j['id']}",
                    )
                )
        return out
