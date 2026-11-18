"""Shared by the vendors whose list page names the postings but does not carry
their text: the text is on each posting's own page, usually as JSON-LD
(schema.org JobPosting). Only postings not seen before are fetched, a few per
run, so a big board fills in over several syncs instead of in one burst."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from typing import Callable

from .. import http
from .. import salary
from ..models import Job, clean_html

LD = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
EMPTY = {"", "none", "null", "unavailable"}  # what pages print for "no value"
DETAIL_PER_RUN = 150  # pages fetched per sync; the rest follow on later runs
DETAIL_THREADS = 4


def json_ld_posting(html: str) -> dict | None:
    """The JobPosting object of a page, or None. Pages carry several JSON-LD
    blocks (organisation, breadcrumbs): the first JobPosting wins."""
    for m in LD.finditer(html or ""):
        try:
            data = json.loads(m.group(1), strict=False)  # raw newlines
        except ValueError:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else data
        for it in items if isinstance(items, list) else [items]:
            if isinstance(it, dict) and it.get("@type") == "JobPosting":
                return it
    return None


def ld_location(it: dict) -> str:
    locs = it.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    parts = []
    for loc in locs:
        a = (loc or {}).get("address") or {}
        if isinstance(a, str):
            parts.append(a)
            continue
        bits = [
            a.get("addressLocality"),
            a.get("addressRegion"),
            a.get("addressCountry"),
        ]
        txt = ", ".join(
            str(b.get("name") if isinstance(b, dict) else b)
            for b in bits
            if b and str(b).strip().lower() not in EMPTY
        )
        if txt:
            parts.append(txt)
    out = " / ".join(dict.fromkeys(parts))
    if str(it.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        out = ("Remote " + out).strip()
    return out


def ld_fields(it: dict) -> dict:
    """A JobPosting as the plain fields of a Job (only those it has)."""
    org = it.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else org
    description = clean_html(it.get("description"))
    if description and not salary.parse(description):
        # pay given only as structured data: say it in the text
        pay = salary.from_schema_org(it.get("baseSalary"))
        description = f"{description}\n\n{pay}" if pay else description
    out = {
        "title": unescape(str(it.get("title") or "")).strip(),
        "location": ld_location(it),
        "posted_at": str(it.get("datePosted") or ""),
        "description": description,
        "company": ""
        if str(company or "").strip().lower() in EMPTY
        else unescape(str(company)).strip(),
    }
    return {k: v for k, v in out.items() if v}


def page_detail(url: str, **kw) -> dict:
    """Detail fetcher for the common case: the posting page's JSON-LD."""
    try:
        r = http.get(url, retries=2, **kw)
        if r.status_code != 200:
            return {}
        it = json_ld_posting(r.text)
        return ld_fields(it) if it else {}
    except Exception:  # one posting page failing is not a failed board
        return {}


def enrich(
    listed: list[Job],
    previous: dict[str, Job] | None,
    detail: Callable[[str], dict],
    *,
    per_run: int = DETAIL_PER_RUN,
    threads: int = DETAIL_THREADS,
) -> list[Job]:
    """`listed` is what the list page gave (title, url, maybe location).
    Fetch the page of each posting that has no text stored yet, and fill in
    what it says; postings not fetched this time keep what they had."""
    previous = previous or {}
    todo = [
        j.url
        for j in listed
        if not j.description
        and (j.url not in previous or not previous[j.url].description)
    ][:per_run]
    got: dict[str, dict] = {}
    if todo:
        with ThreadPoolExecutor(threads, thread_name_prefix="detail") as ex:
            for u, d in zip(todo, ex.map(detail, todo)):
                if d:
                    got[u] = d
    for j in listed:
        d, old = got.get(j.url), previous.get(j.url)
        if d:
            for field in ("title", "location", "posted_at", "description"):
                if d.get(field):
                    setattr(j, field, d[field])
            if d.get("company"):
                j.company = d["company"]
        elif old and not j.description:
            j.description = old.description
            j.posted_at = j.posted_at or old.posted_at
            j.location = j.location or old.location
            if old.company and old.company != j.board:
                j.company = old.company
    return listed


_TAGS = re.compile(r"<[^>]+>")


def text(s: str | None) -> str:
    """One line of plain text from a snippet of HTML."""
    return re.sub(r"\s+", " ", unescape(_TAGS.sub(" ", s or ""))).strip()


def get_page(url: str, board: str, **kw) -> str:
    """The body of a list page; BoardGone when the tenant does not exist."""
    from .base import BoardGone

    r = http.get(url, **kw)
    if r.status_code in (404, 410):
        raise BoardGone(board)
    r.raise_for_status()
    return r.text


def jobs_of(vendor: str, board: str, rows, company: str = "") -> list[Job]:
    """Jobs from [(url, title, location)] rows of a list page."""
    return [
        Job(
            vendor=vendor,
            board=board,
            title=title,
            company=company or board,
            location=loc,
            url=url,
        )
        for url, title, loc in rows
    ]


_DIV = re.compile(r"<(/?)div\b[^>]*>", re.I)


def div_inner(html: str, marker: re.Pattern | str) -> str:
    """The inside of the first <div> whose opening tag matches `marker`
    (balanced, so nested divs stay in), or ''."""
    m = (
        re.search(marker, html)
        if isinstance(marker, str)
        else marker.search(html)
    )
    if not m:
        return ""
    start = html.rfind("<div", 0, m.end())
    open_tag = _DIV.match(html, start)
    if not open_tag:
        return ""
    depth = 1
    for t in _DIV.finditer(html, open_tag.end()):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return html[open_tag.end() : t.start()]
    return ""
