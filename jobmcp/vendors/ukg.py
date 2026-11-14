"""UltiPro and UKG recruiting: one engine on two kinds of host.

    https://recruiting.ultipro.com/<tenant>/JobBoard/<id>
    https://<sub>.rec.pro.ukg.net/<company>/JobBoard/<id>

POST .../JobBoardView/LoadSearchResults lists the postings (with a short
teaser), and a posting's page embeds the whole record as JSON.
"""

from __future__ import annotations

import json
import re

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SoftBlocked, Vendor, valid_slug
from .detail import enrich

PAGE = 50
MAX_PAGES = 40
GUID = re.compile(r"^[0-9a-fA-F-]{36}$")
DETAIL = re.compile(r"CandidateOpportunityDetail\(\s*")
SEARCH = {
    "opportunitySearch": {
        "Top": PAGE,
        "Skip": 0,
        "QueryString": "",
        "OrderBy": [
            {
                "Value": "postedDateDesc",
                "PropertyName": "PostedDate",
                "Ascending": False,
            }
        ],
        "Filters": [
            {
                "t": "TermsSearchFilterDto",
                "fieldName": n,
                "extra": None,
                "values": [],
            }
            for n in (4, 5, 6, 37)
        ],
    },
    "matchCriteria": {
        "PreferredJobs": [],
        "Educations": [],
        "LicenseAndCertifications": [],
        "Skills": [],
        "hasNoLicenses": False,
        "SkippedSkills": [],
    },
}


def _location(o: dict) -> str:
    names = []
    for loc in o.get("Locations") or []:
        a = loc.get("Address") or {}
        city = a.get("City") or ""
        state = (a.get("State") or {}).get("Code") or ""
        country = (a.get("Country") or {}).get("Name") or ""
        place = ", ".join(x for x in (city, state) if x)
        label = (
            f"{place}, {country}"
            if place and country
            else place
            or loc.get("LocalizedDescription")
            or loc.get("LocalizedName")
            or country
        )
        if label and label not in names:
            names.append(label)
    where = " / ".join(names)
    if str(o.get("JobLocationType") or "") in ("1", "Remote"):
        where = f"Remote {where}".strip()
    return where


class _Engine(Vendor):
    """Subclasses say where a board lives and how its key is written."""

    case_sensitive = True
    uses_previous = True

    def base(self, board: str) -> str:  # https://host/<company>/JobBoard/<id>
        raise NotImplementedError

    def fetch(self, board, previous=None):
        base = self.base(board)
        listed, teaser, skip = [], {}, 0
        for _ in range(MAX_PAGES):
            body = json.loads(json.dumps(SEARCH))
            body["opportunitySearch"]["Skip"] = skip
            r = http.post(
                f"{base}/JobBoardView/LoadSearchResults",
                json=body,
                headers={"Accept": "application/json"},
            )
            if r.status_code in (404, 410):
                raise BoardGone(board)
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                raise SoftBlocked(f"{board}: the answer is not JSON") from None
            items = data.get("opportunities") or []
            for o in items:
                if not o.get("Id"):
                    continue
                job = Job(
                    vendor=self.name,
                    board=board,
                    title=(o.get("Title") or "").strip(),
                    company=board.split("/")[0],
                    location=_location(o),
                    url=f"{base}/OpportunityDetail?opportunityId={o['Id']}",
                    posted_at=o.get("PostedDate") or "",
                )
                teaser[job.url] = clean_html(o.get("BriefDescription"))
                listed.append(job)
            skip += PAGE
            if len(items) < PAGE or skip >= (data.get("totalCount") or 0):
                break
        out = enrich(listed, previous, self._detail)
        for j in out:  # the teaser beats nothing, when the page was not read
            j.description = j.description or teaser.get(j.url, "")
        return out

    @staticmethod
    def _detail(url: str) -> dict:
        try:
            r = http.get(url, retries=2)
            if r.status_code != 200:
                return {}
            m = DETAIL.search(r.text)
            if not m:
                return {}
            d, _ = json.JSONDecoder().raw_decode(r.text, m.end())
            body = clean_html(d.get("Description"))
            eeo = clean_html(d.get("EqualOpportunityEmployerDescription"))
            return {"description": f"{body}\n\n{eeo}".strip() if eeo else body}
        except Exception:  # one posting page failing is not a failed board
            return {}


class UltiPro(_Engine):
    name = "ultipro"
    label = "UltiPro"
    example = "BRU1001BRUCK/40c1c711-7dcf-4fbb-9254-e2e3d7d2b1aa"
    hint = (
        "It is the two parts around /JobBoard/ in the careers URL: "
        "recruiting.ultipro.com/<tenant>/JobBoard/<id> -> <tenant>/<id>."
    )
    cdx_queries = (("recruiting.ultipro.com", "domain"),)

    def base(self, board):
        tenant, guid = board.split("/")
        return f"https://recruiting.ultipro.com/{tenant}/JobBoard/{guid}"

    def public_url(self, board):
        return self.base(board)

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/")
        m = re.search(
            r"ultipro\.com/([^/]+)/JobBoard/([0-9a-fA-F-]{36})", s, re.I
        )
        if m:
            return f"{m.group(1)}/{m.group(2).lower()}"
        parts = s.split("/")
        if len(parts) == 2 and valid_slug(parts[0]) and GUID.match(parts[1]):
            return f"{parts[0]}/{parts[1].lower()}"
        raise ValueError(
            f"{slug!r} is not a valid UltiPro board name. {self.hint}"
        )

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != "recruiting.ultipro.com" or len(segs) < 3:
            return None
        if segs[1].lower() != "jobboard" or not GUID.match(segs[2]):
            return None
        return f"{segs[0]}/{segs[2].lower()}" if valid_slug(segs[0]) else None


class UKG(_Engine):
    name = "ukg"
    label = "UKG Pro Recruiting"
    example = "teamjck/JCK1000JCKR/c6d7f87d-c135-41ba-8800-ae0483099838"
    hint = (
        "It is the subdomain and the two parts around /JobBoard/ in the "
        "careers URL: <sub>.rec.pro.ukg.net/<company>/JobBoard/<id> -> "
        "<sub>/<company>/<id>."
    )
    cdx_queries = (("rec.pro.ukg.net", "domain"),)

    def base(self, board):
        sub, company, guid = board.split("/")
        return f"https://{sub}.rec.pro.ukg.net/{company}/JobBoard/{guid}"

    def public_url(self, board):
        return self.base(board)

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/")
        m = re.search(
            r"//([a-z0-9-]+)\.rec\.pro\.ukg\.net/([^/]+)/JobBoard/"
            r"([0-9a-fA-F-]{36})",
            s,
            re.I,
        )
        if m:
            return f"{m.group(1).lower()}/{m.group(2)}/{m.group(3).lower()}"
        p = s.split("/")
        if (
            len(p) == 3
            and valid_slug(p[0])
            and valid_slug(p[1])
            and GUID.match(p[2])
        ):
            return f"{p[0].lower()}/{p[1]}/{p[2].lower()}"
        raise ValueError(
            f"{slug!r} is not a valid UKG board name. {self.hint}"
        )

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if not host.endswith(".rec.pro.ukg.net") or len(segs) < 3:
            return None
        sub = host[: -len(".rec.pro.ukg.net")]
        if "." in sub or not valid_slug(sub) or not valid_slug(segs[0]):
            return None
        if segs[1].lower() != "jobboard" or not GUID.match(segs[2]):
            return None
        return f"{sub}/{segs[0]}/{segs[2].lower()}"
