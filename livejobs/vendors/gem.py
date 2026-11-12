from __future__ import annotations

from .. import http
from ..models import Job, clean_html, normalize_text
from .base import Vendor, valid_slug


class Gem(Vendor):
    name = "gem"
    label = "Gem"
    example = "function-health"
    hint = "It is the first part of the careers URL: jobs.gem.com/<slug>."
    public_url_tpl = "https://jobs.gem.com/{board}"
    cdx_queries = (("jobs.gem.com", "domain"),)

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "jobs.gem.com" or not segs or not valid_slug(segs[0]):
            return None
        return segs[0].lower()

    @staticmethod
    def _location(j: dict) -> str:
        loc = (j.get("location") or {}).get("name") or ""
        # Gem keeps remote/hybrid in a field of its own; put it in the text
        # so the workplace label sees it.
        kind = j.get("location_type")
        if kind in ("remote", "hybrid") and kind not in loc.lower():
            loc = f"{loc} ({kind.capitalize()})" if loc else kind.capitalize()
        return loc

    def fetch(self, board):
        r = http.get(f"https://api.gem.com/job_board/v0/{board}/job_posts/")
        r.raise_for_status()
        out = []
        for j in r.json():
            # The HTML keeps paragraphs and bullets; the plain-text field
            # runs them together.
            desc = clean_html(j.get("content")) or normalize_text(
                j.get("content_plain")
            )
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=board,  # the API does not name the company
                    location=self._location(j),
                    url=j.get("absolute_url") or "",
                    posted_at=j.get("first_published_at")
                    or j.get("created_at")
                    or "",
                    description=desc,
                )
            )
        return out
