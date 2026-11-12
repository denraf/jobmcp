from __future__ import annotations

from .. import http
from ..models import Job
from .base import Vendor, valid_slug

PAGE = 100


class SmartRecruiters(Vendor):
    name = "smartrecruiters"
    label = "SmartRecruiters"
    example = "BoschGroup"
    hint = (
        "It is the company id in jobs.smartrecruiters.com/<CompanyId>; the "
        "capitalisation matters."
    )
    case_sensitive = True
    public_url_tpl = "https://jobs.smartrecruiters.com/{board}"
    cdx_queries = (
        ("jobs.smartrecruiters.com", "domain"),
        ("careers.smartrecruiters.com", "domain"),
    )

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if (
            host
            not in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com")
            or not segs
        ):
            return None
        if segs[0] == "oneclick-ui":  # /oneclick-ui/company/{id}/...
            return (
                segs[2]
                if len(segs) > 2
                and segs[1] == "company"
                and valid_slug(segs[2])
                else None
            )
        return segs[0] if valid_slug(segs[0]) else None  # case-sensitive

    def fetch(self, board):
        out, offset = [], 0
        while True:
            r = http.get(
                f"https://api.smartrecruiters.com/v1/companies/{board}/postings",
                params={"limit": PAGE, "offset": offset},
            )
            r.raise_for_status()
            data = r.json()
            posts = data.get("content", [])
            for j in posts:
                loc = j.get("location") or {}
                where = ", ".join(
                    x
                    for x in (
                        loc.get("city"),
                        loc.get("region"),
                        (loc.get("country") or "").upper(),
                    )
                    if x
                )
                if loc.get("remote"):
                    where = f"Remote - {where}" if where else "Remote"
                out.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(j.get("name") or "").strip(),
                        company=(j.get("company") or {}).get("name") or board,
                        location=where,
                        url=f"https://jobs.smartrecruiters.com/{board}/{j.get('id')}",
                        posted_at=j.get("releasedDate") or "",
                    )
                )
            offset += PAGE
            if not posts or offset >= data.get("totalFound", 0):
                break
        return out
