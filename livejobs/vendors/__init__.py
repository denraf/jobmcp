"""Vendor registry. To add an ATS: write a Vendor subclass and list it here."""

from __future__ import annotations

from .ashby import Ashby
from .bamboohr import BambooHR
from .base import Vendor
from .breezy import Breezy
from .gem import Gem
from .greenhouse import Greenhouse
from .icims import ICIMS
from .lever import Lever
from .paylocity import Paylocity
from .personio import Personio
from .recruitee import Recruitee
from .smartrecruiters import SmartRecruiters
from .teamtailor import Teamtailor
from .workable import Workable
from .workday import Workday

VENDORS: dict[str, Vendor] = {
    v.name: v
    for v in (
        Greenhouse(),
        Lever(),
        Ashby(),
        Workable(),
        SmartRecruiters(),
        Recruitee(),
        BambooHR(),
        Personio(),
        Teamtailor(),
        Breezy(),
        Workday(),
        ICIMS(),
        Paylocity(),
        Gem(),
    )
}


def classify(url: str) -> tuple[str, str] | None:
    """(vendor, board) for any URL, or None."""
    for v in VENDORS.values():
        board = v.board_from_url(url)
        if board:
            return v.name, board
    return None
