from __future__ import annotations

from .. import http, salary
from ..models import Job, normalize_text
from .base import Vendor, valid_slug


class Ashby(Vendor):
    name = "ashby"
    label = "Ashby"
    example = "1password"
    hint = "It is the first part of the careers URL: jobs.ashbyhq.com/<slug>."
    case_sensitive = True
    public_url_tpl = "https://jobs.ashbyhq.com/{board}"
    cdx_queries = (("jobs.ashbyhq.com", "domain"),)

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "jobs.ashbyhq.com" or not segs or not valid_slug(segs[0]):
            return None
        return segs[
            0
        ]  # slugs are case-sensitive enough that we keep the casing

    def fetch(self, board):
        r = http.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{board}",
            params={"includeCompensation": "true"},
        )
        r.raise_for_status()
        out = []
        for j in r.json().get("jobs", []):
            if j.get("isListed") is False:
                continue
            desc = normalize_text(j.get("descriptionPlain"))
            if not salary.parse(desc):  # pay given only as a field
                pay = salary.from_ashby(j.get("compensation"))
                desc = f"{desc}\n\n{pay}" if pay and desc else desc or pay
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    # Ashby titles routinely carry leading whitespace.
                    title=(j.get("title") or "").strip(),
                    company=board,
                    location=j.get("location") or "",
                    url=j.get("jobUrl") or "",
                    posted_at=j.get("publishedAt") or "",
                    description=desc,
                )
            )
        return out
