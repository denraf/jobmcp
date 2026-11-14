"""Pinpoint: https://<slug>.pinpointhq.com/postings.json lists the open
postings with their full text."""

from __future__ import annotations

from .. import http
from ..models import Job, clean_html
from .base import SubdomainVendor

# the posting's text fields, in reading order (the header sits beside each)
SECTIONS = (
    ("description", None),
    ("key_responsibilities", "key_responsibilities_header"),
    ("skills_knowledge_expertise", "skills_knowledge_expertise_header"),
    ("benefits", "benefits_header"),
)


class Pinpoint(SubdomainVendor):
    name = "pinpoint"
    label = "Pinpoint"
    example = "aab"
    hint = "It is the subdomain: <slug>.pinpointhq.com."
    public_url_tpl = "https://{board}.pinpointhq.com"
    base_domains = ("pinpointhq.com",)

    @staticmethod
    def _description(j: dict) -> str:
        parts = []
        for field, header in SECTIONS:
            body = clean_html(j.get(field))
            if not body:
                continue
            head = (j.get(header) or "").strip() if header else ""
            parts.append(f"{head}\n{body}" if head else body)
        return "\n\n".join(parts)

    @staticmethod
    def _location(loc: dict | None) -> str:
        loc = loc or {}
        parts = [loc.get("city"), loc.get("province"), loc.get("name")]
        return ", ".join(dict.fromkeys(p for p in parts if p))

    def fetch(self, board):
        r = http.get(f"https://{board}.pinpointhq.com/postings.json")
        r.raise_for_status()
        out = []
        for j in r.json().get("data", []):
            where = self._location(j.get("location"))
            if j.get("workplace_type") == "remote" and "remote" not in where:
                where = f"Remote {where}".strip()
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=board,
                    location=where,
                    url=j.get("url") or "",
                    posted_at="",
                    description=self._description(j),
                )
            )
        return out
