"""Jibe (JibeApply): https://<slug>.jibeapply.com/api/jobs lists the
postings, 10 a page, with their text."""

from __future__ import annotations

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SubdomainVendor

MAX_PAGES = 150


class JibeApply(SubdomainVendor):
    name = "jibeapply"
    label = "Jibe"
    example = "navistar"
    hint = "It is the subdomain: <slug>.jibeapply.com."
    public_url_tpl = "https://{board}.jibeapply.com"
    base_domains = ("jibeapply.com",)

    @staticmethod
    def _description(j: dict) -> str:
        parts = [
            clean_html(j.get(k))
            for k in ("description", "responsibilities", "qualifications")
        ]
        return "\n\n".join(p for p in parts if p)

    def fetch(self, board):
        origin = f"https://{board}.jibeapply.com"
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            r = http.get(
                f"{origin}/api/jobs",
                params={
                    "page": page,
                    "sortBy": "relevance",
                    "descending": "false",
                    "internal": "false",
                },
            )
            if r.status_code in (404, 410):
                raise BoardGone(board)
            r.raise_for_status()
            data = r.json() or {}
            items = [(x.get("data") or x) for x in data.get("jobs") or []]
            fresh = [j for j in items if j.get("slug") not in seen]
            for j in fresh:
                seen.add(j.get("slug"))
                ref = j.get("slug") or j.get("req_id")
                where = j.get("full_location") or ", ".join(
                    x for x in (j.get("city"), j.get("state")) if x
                )
                out.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(j.get("title") or "").strip(),
                        company=j.get("hiring_organization")
                        or j.get("brand")
                        or board,
                        location=where,
                        url=f"{origin}/jobs/{ref}",
                        posted_at=j.get("posted_date")
                        or j.get("create_date")
                        or "",
                        description=self._description(j),
                    )
                )
            if not fresh or len(out) >= (data.get("totalCount") or 0):
                break
        return out
