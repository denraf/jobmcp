from __future__ import annotations

from datetime import datetime, timezone

from .. import http
from ..models import Job, clean_html, normalize_text
from .base import Vendor, valid_slug


class Lever(Vendor):
    """EU-hosted boards are keyed as 'eu/{slug}' because they use a different
    API host."""

    name = "lever"
    label = "Lever"
    example = "spotify"
    hint = (
        "It is the first part of the careers URL: jobs.lever.co/<slug>. "
        "EU-hosted boards are eu/<slug>."
    )
    cdx_queries = (("jobs.lever.co", "domain"), ("jobs.eu.lever.co", "domain"))

    def normalize_board(self, slug):
        raw = (slug or "").strip()
        region, _, rest = raw.strip("/").partition("/")
        if region.lower() == "eu" and raw.lower().startswith("eu/"):
            if not rest.strip("/"):
                raise ValueError(
                    f"{slug!r} has the EU prefix but no board name. "
                    f"{self.hint}"
                )
            return "eu/" + super().normalize_board(rest)
        return super().normalize_board(raw)

    def public_url(self, board):
        region, _, slug = board.rpartition("/")
        return f"https://jobs.{'eu.' if region == 'eu' else ''}lever.co/{slug}"

    def board_from_url(self, url):
        host, segs, _ = self.split(url)
        if (
            host not in ("jobs.lever.co", "jobs.eu.lever.co")
            or not segs
            or not valid_slug(segs[0])
        ):
            return None
        slug = segs[0].lower()
        return f"eu/{slug}" if host == "jobs.eu.lever.co" else slug

    @staticmethod
    def _description(j: dict) -> str:
        """Lever splits a posting into parts. `descriptionPlain` is only the
        introduction: what the role asks for lives
        in `lists` (e.g. "Who You Are") and the closing text in `additional`.
        All of it is the description."""
        parts = [j.get("openingPlain"), j.get("descriptionPlain")]
        for section in j.get("lists") or []:
            heading = (section.get("text") or "").strip()
            parts.append(
                f"{heading}\n{clean_html(section.get('content'))}".strip()
            )
        parts.append(j.get("additionalPlain"))
        return "\n\n".join(normalize_text(p) for p in parts if p and p.strip())

    def fetch(self, board):
        region, _, slug = board.rpartition("/")
        host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
        r = http.get(f"https://{host}/v0/postings/{slug}?mode=json")
        r.raise_for_status()
        out = []
        for j in r.json():
            ts = j.get("createdAt")
            out.append(
                Job(
                    vendor=self.name,
                    board=board,
                    title=(j.get("text") or "").strip(),
                    company=slug,
                    location=(j.get("categories") or {}).get("location") or "",
                    url=j.get("hostedUrl") or "",
                    posted_at=datetime.fromtimestamp(
                        ts / 1000, timezone.utc
                    ).isoformat()
                    if ts
                    else "",
                    description=self._description(j),
                )
            )
        return out
