from __future__ import annotations

import re
from datetime import date, timedelta

from .. import http
from ..models import Job
from .base import SoftBlocked, Vendor, valid_slug

HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")
NOT_SITES = {
    "wday",
    "login",
    "assets",
    "favicon.ico",
    "robots.txt",
    "d",
    "cxs",
}
MAX_PER_BOARD = (
    400  # tenants can hold thousands; warn rather than silently truncate
)
PAGE = 20


class Workday(Vendor):
    """Board key: '{tenant}.{pod}/{site}', e.g.
    'nvidia.wd5/NVIDIAExternalCareerSite'."""

    name = "workday"
    label = "Workday"
    example = "nvidia.wd5/NVIDIAExternalCareerSite"
    hint = "tenant.wdN/site, from tenant.wdN.myworkdayjobs.com/<site>."
    cdx_queries = (("myworkdayjobs.com", "domain"),)

    _KEY = re.compile(r"^([a-z0-9-]+)\.(wd\d+)/([A-Za-z0-9_.-]+)$")

    def normalize_board(self, slug):
        m = self._KEY.match((slug or "").strip().strip("/"))
        if not m or m.group(3).lower() in NOT_SITES:
            raise ValueError(
                f"{slug!r} is not a valid Workday board. {self.hint}"
            )
        # the site name keeps its casing
        return f"{m.group(1)}.{m.group(2)}/{m.group(3)}"

    def public_url(self, board):
        host, site = board.split("/", 1)
        tenant, pod = host.split(".", 1)
        return f"https://{tenant}.{pod}.myworkdayjobs.com/{site}"

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        m = HOST.match(host)
        if not m:
            return None
        if segs and LOCALE.match(segs[0]):
            segs = segs[1:]
        if not segs or segs[0].lower() in NOT_SITES or not valid_slug(segs[0]):
            return None
        return f"{m.group(1)}.{m.group(2)}/{segs[0]}"

    def fetch(self, board):
        host_part, site = board.split("/", 1)
        tenant, pod = host_part.split(".", 1)
        host = f"{tenant}.{pod}.myworkdayjobs.com"
        api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        out, offset, total = [], 0, None
        while offset < MAX_PER_BOARD:
            r = http.post(
                api,
                json={
                    "limit": PAGE,
                    "offset": offset,
                    "searchText": "",
                    "appliedFacets": {},
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
            posts = data.get("jobPostings", [])
            now_total = data.get("total")
            # Workday states the total on the first page only and reports 0
            # on the rest, so a zero is "not given", not "the board shrank".
            # What does signal throttling is a different non-zero total, or
            # an empty page before the first page's total has been reached:
            # taking either as the whole board would close every posting past
            # that point.
            if total and now_total and now_total != total:
                raise SoftBlocked(
                    f"{board}: total changed from {total} to {now_total} "
                    f"at offset {offset}"
                )
            if offset and not posts and total and offset < total:
                raise SoftBlocked(
                    f"{board}: empty page at offset {offset} of {total}"
                )
            total = total or now_total
            for j in posts:
                out.append(
                    Job(
                        vendor=self.name,
                        board=board,
                        title=(j.get("title") or "").strip(),
                        company=tenant,
                        location=j.get("locationsText") or "",
                        url=(
                            f"https://{host}/{site}{j.get('externalPath', '')}"
                        ),
                        posted_at=_posted(j.get("postedOn")),
                    )
                )
            offset += PAGE
            if len(posts) < PAGE:
                break
        return out


def _posted(s: str | None, today: date | None = None) -> str:
    """'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> ISO
    date."""
    today = today or date.today()
    s = (s or "").lower()
    if "today" in s:
        return today.isoformat()
    if "yesterday" in s:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", s)
    return (today - timedelta(days=int(m.group(1)))).isoformat() if m else ""
