"""Rippling Recruiting: ats.rippling.com/api/v2/board/<slug>/jobs lists the
postings (20 a page) and .../jobs/<id> carries the text of one."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, Vendor, valid_slug

API = "https://ats.rippling.com/api/v2/board"
PAGE = 100
MAX_PAGES = 25
DETAIL_PER_RUN = 150
DETAIL_THREADS = 4


class Rippling(Vendor):
    name = "rippling"
    label = "Rippling"
    example = "astra"
    hint = "It is the first part of the careers URL: ats.rippling.com/<slug>."
    public_url_tpl = "https://ats.rippling.com/{board}/jobs"
    cdx_queries = (("ats.rippling.com", "domain"),)
    uses_previous = True

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "ats.rippling.com" or not segs:
            return None
        if segs[0].lower() == "api":  # /api/v2/board/<slug>/jobs
            segs = segs[3:4]
        if not segs or not valid_slug(segs[0]):
            return None
        return segs[0].lower()

    @staticmethod
    def _location(locations) -> str:
        names = []
        for loc in locations or []:
            label = loc.get("name") or ", ".join(
                x for x in (loc.get("city"), loc.get("state")) if x
            )
            if label and label not in names:
                names.append(label)
        return " / ".join(names)

    def fetch(self, board, previous=None):
        previous = previous or {}
        listed = []
        for page in range(MAX_PAGES):
            r = http.get(
                f"{API}/{board}/jobs",
                params={"page": page, "pageSize": PAGE},
            )
            if r.status_code == 404:
                raise BoardGone(board)
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            listed += items
            if page + 1 >= (data.get("totalPages") or 1) or not items:
                break
        todo = [
            j["id"]
            for j in listed
            if j.get("url") not in previous
            or not previous[j["url"]].description
        ][:DETAIL_PER_RUN]
        details = {}
        if todo:
            with ThreadPoolExecutor(DETAIL_THREADS) as ex:
                details = dict(
                    zip(todo, ex.map(lambda i: self._detail(board, i), todo))
                )
        out = []
        for j in listed:
            d = details.get(j["id"]) or {}
            old = previous.get(j.get("url"))
            desc = d.get("description") or (old.description if old else "")
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("name") or "").strip(),
                    company=(d.get("company") or "").strip()
                    or (old.company if old and old.company != board else "")
                    or board,
                    location=self._location(j.get("locations")),
                    url=j.get("url") or "",
                    posted_at=d.get("posted_at")
                    or (old.posted_at if old else ""),
                    description=desc,
                )
            )
        return out

    def _detail(self, board: str, job_id: str) -> dict:
        try:
            r = http.get(f"{API}/{board}/jobs/{job_id}", retries=2)
            if r.status_code != 200:
                return {}
            d = r.json()
            text = d.get("description") or {}
            body = "\n\n".join(
                clean_html(text.get(k)) for k in ("company", "role")
            ).strip()
            return {
                "description": body,
                "company": d.get("companyName") or "",
                "posted_at": d.get("createdOn") or "",
            }
        except Exception:  # one posting failing is not a failed board
            return {}
