from __future__ import annotations

import xml.etree.ElementTree as ET

from .. import http
from ..models import Job, clean_html
from .base import Vendor, valid_slug

DOMAINS = ("jobs.personio.de", "jobs.personio.com")


class Personio(Vendor):
    """Boards at {slug}.jobs.personio.de|com; the XML feed is the public API.

    The key keeps the TLD ('acme.de') because the same slug can exist on both.
    """

    name = "personio"
    label = "Personio"
    example = "acme.de"
    hint = (
        "Subdomain plus country: acme.jobs.personio.de is acme.de, "
        "acme.jobs.personio.com is acme.com."
    )
    cdx_queries = tuple((d, "domain") for d in DOMAINS)

    def normalize_board(self, slug):
        s = (slug or "").strip().strip("/").lower()
        sub, _, tld = s.rpartition(".")
        if tld not in ("de", "com") or not valid_slug(sub):
            raise ValueError(
                f"{slug!r} is not a valid Personio board. {self.hint}"
            )
        return s

    def public_url(self, board):
        slug, tld = board.rsplit(".", 1)
        return f"https://{slug}.jobs.personio.{tld}"

    def board_from_url(self, url):
        host, _, _ = self.split(url)
        for d in DOMAINS:
            if host.endswith("." + d):
                sub = host[: -len(d) - 1]
                if "." not in sub and valid_slug(sub):
                    return f"{sub.lower()}.{d.rsplit('.', 1)[1]}"
        return None

    def fetch(self, board):
        slug, tld = board.rsplit(".", 1)
        base = f"https://{slug}.jobs.personio.{tld}"
        r = http.get(f"{base}/xml", params={"language": "en"})
        r.raise_for_status()
        out = []
        for p in ET.fromstring(r.content).iter("position"):

            def t(tag):
                return (p.findtext(tag) or "").strip()

            # Each section is a heading plus an HTML body; keep them as
            # separate blocks.
            desc = "\n\n".join(
                (
                    f"{(d.findtext('name') or '').strip()}\n"
                    f"{clean_html(d.findtext('value'))}"
                ).strip()
                for d in p.iter("jobDescription")
            )
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=t("name"),
                    company=slug,
                    location=t("office"),
                    url=f"{base}/job/{t('id')}",
                    posted_at=t("createdAt"),
                    description=desc,
                )
            )
        return out
