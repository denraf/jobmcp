"""iCIMS: a sitemap lists a tenant's postings and each posting page carries
its details as JSON-LD (schema.org JobPosting).

Board key: the tenant's whole subdomain, e.g. 'careers-acme' for
careers-acme.icims.com (some tenants use another prefix, or none, so the key
says exactly which host to ask).
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from urllib.parse import unquote

from .. import http
from ..models import Job, clean_html
from .base import SoftBlocked, Vendor, valid_slug

HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.icims\.com$")
JOB_PATH = re.compile(r"/jobs/(\d+)(?:/([^/]*))?(?:/job)?/?$")
LD = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.S | re.I,
)
MAX_PER_BOARD = 1000  # postings listed
DETAIL_PER_RUN = 150  # pages fetched per sync; the rest follow on later runs
DETAIL_THREADS = 4
NOT_TENANTS = {"www", "login", "app", "api", "careers", "jobs", "static"}


class ICIMS(Vendor):
    name = "icims"
    label = "iCIMS"
    example = "careers-acme"
    hint = (
        "It is the subdomain of the careers site: careers-acme.icims.com "
        "-> careers-acme."
    )
    public_url_tpl = "https://{board}.icims.com/jobs/search"
    cdx_queries = (("icims.com", "domain"),)
    uses_previous = True

    def normalize_board(self, slug):
        s = (slug or "").strip().lower()
        s = re.sub(r"^https?://", "", s).split("/")[0]
        s = s.removesuffix(".icims.com")
        if not s or "." in s or not valid_slug(s) or s in NOT_TENANTS:
            raise ValueError(
                f"{slug!r} is not a valid iCIMS board name. {self.hint}"
            )
        if not s.startswith(("careers-", "jobs-")):
            return "careers-" + s  # the usual host; paste the URL for others
        return s

    def board_from_url(self, url):
        host, _, _ = self.split(url)
        m = HOST.match(host)
        if not m or m.group(1) in NOT_TENANTS or not valid_slug(m.group(1)):
            return None
        return m.group(1)

    # -- parsing -----------------------------------------------------
    @staticmethod
    def sitemap_jobs(xml: str) -> list[tuple[str, str]]:
        """[(url, title guessed from the url)] for every posting page."""
        out = []
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            m = JOB_PATH.search(loc.split("?")[0])
            if not m or loc.endswith(("/jobs/intro", "/jobs/search")):
                continue
            slug = unquote(m.group(2) or "")
            title = slug.replace("-", " ").strip().title() if slug else ""
            out.append((unescape(loc), title))
        return out

    @staticmethod
    def parse_posting(html: str) -> dict:
        """The JobPosting JSON-LD of a posting page, as plain fields."""
        for m in LD.finditer(html):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            items = (
                data.get("@graph", [data]) if isinstance(data, dict) else data
            )
            for it in items:
                if isinstance(it, dict) and it.get("@type") == "JobPosting":
                    return {
                        "title": unescape(str(it.get("title") or "")).strip(),
                        "location": _location(it),
                        "posted_at": str(it.get("datePosted") or ""),
                        "description": clean_html(it.get("description")),
                        "company": (
                            (it.get("hiringOrganization") or {}).get("name")
                        )
                        or "",
                    }
        return {}

    # -- fetching ----------------------------------------------------
    def fetch(self, board, previous=None):
        previous = previous or {}
        base = f"https://{board}.icims.com"
        r = http.get(f"{base}/sitemap.xml")
        r.raise_for_status()
        listed = self.sitemap_jobs(r.text)[:MAX_PER_BOARD]
        if (
            not listed
            and "<urlset" not in r.text
            and "<sitemapindex" not in r.text
        ):
            raise SoftBlocked(f"{board}: the sitemap is not a sitemap")
        todo = [
            u
            for u, _ in listed
            if u not in previous or not previous[u].description
        ][:DETAIL_PER_RUN]
        details: dict[str, dict] = {}
        if todo:
            with ThreadPoolExecutor(
                DETAIL_THREADS, thread_name_prefix="icims"
            ) as ex:
                for u, d in zip(todo, ex.map(self._detail, todo)):
                    if d:
                        details[u] = d
        out = []
        for url, guessed in listed:
            d = details.get(url)
            old = previous.get(url)
            if d:
                title, loc = d["title"] or guessed, d["location"]
                posted, desc = d["posted_at"], d["description"]
                company = d["company"]
            elif old:  # not re-fetched this time: keep what we know
                title, loc, posted = old.title, old.location, old.posted_at
                desc, company = old.description, old.company
            else:
                title, loc, posted, desc, company = guessed, "", "", "", ""
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=title,
                    company=company or board.removeprefix("careers-"),
                    location=loc,
                    url=url,
                    posted_at=posted,
                    description=desc,
                )
            )
        return out

    def _detail(self, url: str) -> dict:
        try:
            r = http.get(url, params={"in_iframe": 1}, retries=2)
            if r.status_code != 200:
                return {}
            return self.parse_posting(r.text)
        except Exception:  # one posting page failing is not a failed board
            return {}


def _location(it: dict) -> str:
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
            str(b) for b in bits if b and str(b).upper() != "UNAVAILABLE"
        )
        if txt:
            parts.append(txt)
    out = " / ".join(dict.fromkeys(parts))
    if str(it.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        out = ("Remote " + out).strip()
    return out
