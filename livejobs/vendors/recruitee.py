from __future__ import annotations

from .. import http
from ..models import Job, clean_html
from .base import SubdomainVendor


class Recruitee(SubdomainVendor):
    name = "recruitee"
    label = "Recruitee"
    example = "bunq"
    hint = "It is the subdomain: <slug>.recruitee.com."
    public_url_tpl = "https://{board}.recruitee.com"
    base_domains = ("recruitee.com",)

    @staticmethod
    def _description(j: dict) -> str:
        """Recruitee keeps the requirements in their own field, apart from the
        description."""
        desc, req = (
            clean_html(j.get("description")),
            clean_html(j.get("requirements")),
        )
        return f"{desc}\n\nRequirements\n{req}" if req else desc

    def fetch(self, board):
        r = http.get(f"https://{board}.recruitee.com/api/offers/")
        r.raise_for_status()
        out = []
        for j in r.json().get("offers", []):
            if j.get("status") not in (None, "published"):
                continue
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("title") or "").strip(),
                    company=j.get("company_name") or board,
                    location=j.get("location")
                    or ", ".join(
                        x for x in (j.get("city"), j.get("country")) if x
                    ),
                    url=j.get("careers_url") or "",
                    posted_at=j.get("published_at")
                    or j.get("created_at")
                    or "",
                    description=self._description(j),
                )
            )
        return out
