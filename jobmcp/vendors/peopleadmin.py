"""PeopleAdmin (higher-education jobs): https://<slug>.peopleadmin.com/
postings/all_jobs.atom is an Atom feed of the open postings with the start of
each text."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .. import http
from ..models import Job, clean_html
from .base import BoardGone, SoftBlocked, SubdomainVendor

ATOM = "{http://www.w3.org/2005/Atom}"
NOT_BOARDS = {"www", "api", "login", "support"}


class PeopleAdmin(SubdomainVendor):
    name = "peopleadmin"
    label = "PeopleAdmin"
    example = "jobs-shepherd"
    hint = "It is the subdomain: <slug>.peopleadmin.com."
    public_url_tpl = "https://{board}.peopleadmin.com/postings/search"
    base_domains = ("peopleadmin.com",)

    def board_from_url(self, url):
        board = super().board_from_url(url)
        return None if board in NOT_BOARDS else board

    @staticmethod
    def parse_feed(xml: str) -> list[Job] | None:
        try:
            root = ET.fromstring(xml.encode("utf-8"))
        except ET.ParseError:
            return None
        if root.tag != f"{ATOM}feed":
            return None
        return root.findall(f"{ATOM}entry")

    def fetch(self, board):
        r = http.get(f"https://{board}.peopleadmin.com/postings/all_jobs.atom")
        if r.status_code in (404, 410):
            raise BoardGone(board)
        r.raise_for_status()
        entries = self.parse_feed(r.text)
        if entries is None:
            raise SoftBlocked(f"{board}: the answer is not an Atom feed")
        out = []
        for e in entries:
            link = e.find(f"{ATOM}link[@rel='alternate']")
            url = (link.get("href") if link is not None else "") or (
                e.findtext(f"{ATOM}id") or ""
            )
            # an empty board answers with a placeholder entry
            if not re.search(r"/postings/\d+", url):
                continue
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(e.findtext(f"{ATOM}title") or "").strip(),
                    company=(
                        e.findtext(f"{ATOM}author/{ATOM}name") or ""
                    ).strip()
                    or board,
                    location="",
                    url=url,
                    posted_at=e.findtext(f"{ATOM}published") or "",
                    description=clean_html(e.findtext(f"{ATOM}content")),
                )
            )
        return out
