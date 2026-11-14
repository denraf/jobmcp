"""AgileHR: <slug>.agilehr.com/public/api/careerportal/getall lists the
postings with their text."""

from __future__ import annotations

import re

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SubdomainVendor


def _date(s) -> str:
    """AgileHR uses 0001-01-01 for 'no date'."""
    s = str(s or "").strip()
    if not s or re.match(r"^(0001|1900|1901)-|^0?1/0?1/(0001|190[01])", s):
        return ""
    return s


class AgileHR(SubdomainVendor):
    name = "agilehr"
    label = "AgileHR"
    example = "acme"
    hint = "It is the subdomain: <slug>.agilehr.com."
    public_url_tpl = "https://{board}.agilehr.com"
    base_domains = ("agilehr.com",)

    def fetch(self, board):
        r = http.get(
            f"https://{board}.agilehr.com/public/api/careerportal/getall",
            params={"sourceId": 0},
        )
        if r.status_code == 404:
            raise BoardGone(board)
        r.raise_for_status()
        data = r.json() or {}
        items = data.get("ResultList")
        if items is None:
            res = data.get("Result")
            items = res if isinstance(res, list) else [res] if res else []
        out = []
        for j in items:
            desc = clean_html(j.get("LongDescription")) or clean_html(
                j.get("Description")
            )
            extra = clean_html(j.get("AdditionalInformation"))
            if extra:
                desc = f"{desc}\n\n{extra}".strip()
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("Title") or "").strip(),
                    company=board,
                    location=(
                        j.get("Location") or j.get("City") or ""
                    ).strip(),
                    url=j.get("ApplyUrl") or "",
                    posted_at=_date(j.get("StartDate"))
                    or _date(j.get("OpenDate")),
                    description=desc,
                )
            )
        return out
