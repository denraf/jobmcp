"""Paylocity Recruiting: the tenant's job list page embeds its postings as a
JSON object (`window.pageData = {...}`).

The list carries only the first ~110 characters of each description; the full
text is on the posting's own page (JSON-LD), fetched for postings not seen
before.

Board key: the tenant's GUID (lower case), the part after /jobs/All/ in the
careers URL. Paylocity does not use readable names.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from html import unescape

from .. import http
from ..models import Job, clean_html
from .base import SoftBlocked, Vendor

GUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
HOST = "recruiting.paylocity.com"
MARK = "window.pageData"
LD = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)
# One request at a time per ~second across every thread: asking for the
# posting pages faster than that gets 429s.
_gate = http.Gate("Paylocity", min_interval=1.0, max_cooldown=120.0)
DETAIL_GIVE_UP_AFTER = 3  # failed pages in a row before this run stops asking
DETAIL_PER_RUN = 150  # pages fetched per sync; the rest follow on later runs
DETAIL_THREADS = 4


class Paylocity(Vendor):
    name = "paylocity"
    label = "Paylocity"
    example = "9597a587-1c23-4def-9ecc-4901e647fab7"
    hint = (
        "It is the GUID in the careers URL: "
        "recruiting.paylocity.com/recruiting/jobs/All/<guid>/..."
    )
    public_url_tpl = (
        "https://recruiting.paylocity.com/recruiting/jobs/All/{board}"
    )
    cdx_queries = ((HOST, "domain"),)
    uses_previous = True  # only postings without their full text are fetched

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/").lower()
        m = re.search(r"/jobs/all/([0-9a-f-]{36})", s)
        s = m.group(1) if m else s
        if not GUID.match(s):
            raise ValueError(
                f"{slug!r} is not a valid Paylocity board name. {self.hint}"
            )
        return s

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if host != HOST:
            return None
        low = [s.lower() for s in segs]
        # /recruiting/jobs/All/<guid>[/<name>]  (also /Career/ and /Details/
        # are skipped: they carry no tenant GUID we can rely on)
        if len(low) >= 4 and low[:3] == ["recruiting", "jobs", "all"]:
            return low[3] if GUID.match(low[3]) else None
        return None

    @staticmethod
    def page_data(html: str) -> dict | None:
        """The object assigned to window.pageData, or None. Decoded by the
        JSON parser from the opening brace, so nested braces and script
        endings inside strings cannot cut it short."""
        i = html.find(MARK)
        if i < 0:
            return None
        j = html.find("{", i)
        if j < 0:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(html[j:])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def fetch(self, board, previous=None):
        previous = previous or {}
        r = http.get(f"https://{HOST}/recruiting/jobs/All/{board}/")
        r.raise_for_status()
        data = self.page_data(r.text)
        if data is None or "Jobs" not in data:
            # 200 but no job list: a block page or a changed layout. Not
            # the same thing as a company with no openings.
            raise SoftBlocked(f"{board}: no job list in the page")
        company = unescape(str(data.get("CompanyName") or "")).strip()
        listed = []
        for j in data.get("Jobs") or []:
            jid = j.get("JobId")
            if not jid:
                continue
            url = f"https://{HOST}/Recruiting/Jobs/Details/{jid}"
            # the list carries the start of the description only
            listed.append((j, url, clean_html(j.get("Description"))))
        # A posting needs its page unless we already hold more than the list's
        # snippet (a stored text equal to the snippet is the cut-off one).
        todo = [
            url
            for _, url, snippet in listed
            if url not in previous
            or previous[url].description.strip() == snippet
        ][:DETAIL_PER_RUN]
        full: dict[str, str] = {}
        orgs: list[str] = []
        if todo:
            run = _Run()
            with ThreadPoolExecutor(
                DETAIL_THREADS, thread_name_prefix="paylocity"
            ) as ex:
                for u, d in zip(
                    todo, ex.map(lambda u: self._detail(u, run), todo)
                ):
                    if d[0]:
                        full[u] = d[0]
                    if d[1]:
                        orgs.append(d[1])
        # The tenant's name: the posting page's organisation if we fetched
        # one now, else what an earlier sync stored (so it does not flip
        # between spellings), else the page's own title.
        company = (
            company
            or (orgs[0] if orgs else "")
            or next(
                (
                    p.company
                    for p in previous.values()
                    if p.company and p.company != board
                ),
                "",
            )
            or _module_name(data)
        )
        out = []
        for j, url, snippet in listed:
            old = previous.get(url)
            desc = full.get(url) or (
                old.description if old and old.description else snippet
            )
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=unescape(j.get("JobTitle") or "").strip(),
                    company=company or board,
                    location=_location(j),
                    url=url,
                    posted_at=str(j.get("PublishedDate") or ""),
                    description=desc,
                )
            )
        return out

    @staticmethod
    def parse_posting(html: str) -> tuple[str, str]:
        """(full text, company name) from the posting page's JobPosting
        JSON-LD."""
        for m in LD.finditer(html):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            for it in data if isinstance(data, list) else [data]:
                if isinstance(it, dict) and it.get("@type") == "JobPosting":
                    org = (it.get("hiringOrganization") or {}).get("name")
                    return (
                        clean_html(it.get("description")),
                        unescape(str(org or "")).strip(),
                    )
        return "", ""

    def _detail(self, url: str, run: "_Run") -> tuple[str, str]:
        if run.stopped:  # the host is refusing us: the rest follow next run
            return "", ""
        try:
            r = http.get(url, retries=2, gate=_gate)
            got = (
                self.parse_posting(r.text)
                if r.status_code == 200
                else ("", "")
            )
        except Exception:  # one posting page failing is not a failed board
            got = ("", "")
        run.record(bool(got[0]))
        return got


class _Run:
    """Counts failed posting pages in a row across the threads of one sync."""

    def __init__(self):
        self._lock, self._bad = threading.Lock(), 0

    def record(self, ok: bool) -> None:
        with self._lock:
            self._bad = 0 if ok else self._bad + 1

    @property
    def stopped(self) -> bool:
        return self._bad >= DETAIL_GIVE_UP_AFTER


def _module_name(data: dict) -> str:
    """The careers page's title, e.g. '116508 Innovation At Work': Paylocity
    puts the customer number in front, which is not part of the name."""
    name = unescape(str(data.get("ModuleTitle") or "")).strip()
    return re.sub(r"^\d+\s+(?=\S)", "", name)


def _location(j: dict) -> str:
    loc = j.get("JobLocation") or {}
    bits = [loc.get("City"), loc.get("State"), loc.get("Country")]
    text = ", ".join(str(b) for b in bits if b)
    if j.get("IsRemote"):
        text = ("Remote " + text).strip()
    return text or (j.get("LocationName") or "")
