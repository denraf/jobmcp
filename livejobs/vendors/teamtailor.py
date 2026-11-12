from __future__ import annotations

import xml.etree.ElementTree as ET

from .. import http
from ..models import Job, clean_html
from .base import SubdomainVendor


class Teamtailor(SubdomainVendor):
    name = "teamtailor"
    label = "Teamtailor"
    example = "acme"
    hint = (
        "It is the subdomain: <slug>.teamtailor.com. (Companies on their own "
        "domain may not work.)"
    )
    public_url_tpl = "https://{board}.teamtailor.com"
    base_domains = ("teamtailor.com",)

    def fetch(self, board):
        r = http.get(f"https://{board}.teamtailor.com/jobs.rss")
        r.raise_for_status()
        out = []
        for item in ET.fromstring(r.content).iter("item"):
            # Location lives in a namespaced
            # <tt:locations><tt:location><tt:city>.
            city = next(
                (
                    e.text
                    for e in item.iter()
                    if e.tag.endswith("}city") and e.text
                ),
                "",
            )
            country = next(
                (
                    e.text
                    for e in item.iter()
                    if e.tag.endswith("}country") and e.text
                ),
                "",
            )
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(item.findtext("title") or "").strip(),
                    company=board,
                    location=", ".join(x for x in (city, country) if x),
                    url=(item.findtext("link") or "").strip(),
                    posted_at=(item.findtext("pubDate") or "").strip(),
                    description=clean_html(item.findtext("description")),
                )
            )
        return out
