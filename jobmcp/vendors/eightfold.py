"""Eightfold: https://<slug>.eightfold.ai/careers names its talent-network
domain in the page (`_EF_GROUP_ID`); /api/pcsx/search lists the postings
(10 a page) and /api/pcsx/position_details returns the text of one."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .. import http
from ..models import Job, clean_html
from .base import SoftBlocked, SubdomainVendor
from .detail import enrich, get_page

GROUP = re.compile(r"_EF_GROUP_ID\s*=\s*[\"']([^\"']+)[\"']", re.I)
PAGE = 10
MAX_PAGES = 150
NOT_BOARDS = {"www", "app", "api", "login", "support"}


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class Eightfold(SubdomainVendor):
    name = "eightfold"
    label = "Eightfold"
    example = "bms"
    hint = "It is the subdomain: <slug>.eightfold.ai."
    public_url_tpl = "https://{board}.eightfold.ai/careers"
    base_domains = ("eightfold.ai",)
    uses_previous = True

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in NOT_BOARDS else board

    def fetch(self, board, previous=None):
        origin = f"https://{board}.eightfold.ai"
        m = GROUP.search(get_page(f"{origin}/careers", board))
        if not m:
            raise SoftBlocked(f"{board}: no talent-network domain in the page")
        domain, listed, total = m.group(1), [], None
        for page in range(MAX_PAGES):
            r = http.get(
                f"{origin}/api/pcsx/search",
                params={
                    "domain": domain,
                    "query": "",
                    "location": "",
                    "start": page * PAGE,
                },
            )
            r.raise_for_status()
            data = (r.json() or {}).get("data") or {}
            positions = data.get("positions") or []
            total = data.get("count", total)
            for p in positions:
                if not p.get("id") or not p.get("positionUrl"):
                    continue
                where = " / ".join(p.get("locations") or [])
                if p.get("workLocationOption") == "remote":
                    where = f"Remote {where}".strip()
                listed.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(p.get("name") or "").strip(),
                        company=board,
                        location=where,
                        url=f"{origin}{p['positionUrl']}",
                        posted_at=_iso(p.get("postedTs")),
                    )
                )
            if len(positions) < PAGE or (page + 1) * PAGE >= (total or 0):
                break

        def detail(url: str) -> dict:
            pid = url.rsplit("/", 1)[-1]
            try:
                d = http.get(
                    f"{origin}/api/pcsx/position_details",
                    params={"position_id": pid, "domain": domain, "hl": "en"},
                    retries=2,
                )
                if d.status_code != 200:
                    return {}
                body = ((d.json() or {}).get("data") or {}).get(
                    "jobDescription"
                )
                return {"description": clean_html(body)}
            except Exception:  # one posting failing is not a failed board
                return {}

        return enrich(listed, previous, detail)
