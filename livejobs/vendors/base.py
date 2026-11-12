from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..models import Job

# Path segments / subdomains that are site furniture, never a company board.
RESERVED = frozenset(
    {
        "",
        "api",
        "static",
        "assets",
        "embed",
        "robots.txt",
        "favicon.ico",
        "sitemap.xml",
        "www",
        "app",
        "apply",
        "careers",
        "jobs",
        "login",
        "signup",
        "v1",
        "v0",
        "health",
        "cdn",
        "images",
        "img",
        "js",
        "css",
        "widget",
        "oauth",
        "auth",
        "support",
        "help",
        "blog",
        "status",
        "mail",
    }
)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


_ASSET = re.compile(
    r"\.(js|css|png|ico|txt|xml|json|svg|jpe?g|gif|woff2?)$", re.I
)


def valid_slug(s: str) -> bool:
    return (
        bool(_SLUG.match(s))
        and s.lower() not in RESERVED
        and not _ASSET.search(s)
    )


class BoardGone(Exception):
    """The board no longer exists (vendor answered, but not with a board)."""


class SoftBlocked(Exception):
    """The vendor answered, but not with the data: a 200 page with the
    postings missing, a total that changes mid-way. Treated as a failed fetch
    (the board keeps its stored postings and is retried later) rather than as
    'this board has no jobs', which would close every posting."""


class Vendor:
    """One ATS. Subclasses answer two questions:

    * `board_from_url` — given a URL seen in Common Crawl, which board
      (company tenant) does it belong to? None if it isn't a board URL.
    * `fetch` — given a board key, return its current postings.
    """

    name: str = ""
    label: str = ""  # display name
    example: str = (
        ""  # an example board key, shown as the placeholder in the admin UI
    )
    hint: str = ""  # one line on where to find the slug
    case_sensitive: bool = (
        False  # keep the casing of the slug as given (Ashby, SmartRecruiters)
    )
    public_url_tpl: str = (
        ""  # the company's public careers page, {board} substituted
    )
    # CDX queries (url + matchType) used to enumerate this vendor in Common
    # Crawl.
    cdx_queries: tuple[tuple[str, str], ...] = ()
    # True for vendors whose list has no details: fetch() is then given the
    # postings already stored for the board (url -> Job) so it only has to
    # fetch the pages it has not seen.
    uses_previous: bool = False

    def board_from_url(self, url: str) -> str | None:
        raise NotImplementedError

    def normalize_board(self, slug: str) -> str:
        """The canonical board key for what an admin typed, or ValueError
        saying what is wrong.
        Canonical matters: the key Common Crawl discovery produces and the one
        typed by hand must be identical,
        or the same board would exist twice."""
        s = (slug or "").strip().strip("/")
        if not valid_slug(s):
            msg = (
                f"{slug!r} is not a valid {self.name} board name. {self.hint}"
            )
            raise ValueError(msg.strip())
        return s if self.case_sensitive else s.lower()

    def public_url(self, board: str) -> str:
        return (
            self.public_url_tpl.format(board=board)
            if self.public_url_tpl
            else ""
        )

    def fetch(self, board: str) -> list[Job]:
        raise NotImplementedError

    # helpers ---------------------------------------------------------
    @staticmethod
    def split(url: str):
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        segs = [s for s in u.path.split("/") if s]
        return host, segs, u.query


class SubdomainVendor(Vendor):
    """Boards live at https://{slug}.{base_domain}/..."""

    base_domains: tuple[str, ...] = ()

    @property
    def cdx_queries(self):  # type: ignore[override]
        return tuple((d, "domain") for d in self.base_domains)

    def board_from_url(self, url: str) -> str | None:
        host, _, _ = self.split(url)
        for d in self.base_domains:
            if host.endswith("." + d):
                sub = host[: -len(d) - 1]
                # a.b.recruitee.com is not a board; only one label deep
                if "." not in sub and valid_slug(sub):
                    return sub.lower()
        return None
