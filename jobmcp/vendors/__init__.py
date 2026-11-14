"""Vendor registry. To add an ATS: write a Vendor subclass and list it here."""

from __future__ import annotations

from .applytojob import ApplyToJob
from .ashby import Ashby
from .bamboohr import BambooHR
from .agilehr import AgileHR
from .careerplug import CareerPlug
from .comeet import Comeet
from .freshteam import Freshteam
from .hibob import HiBob
from .homerun import Homerun
from .join import Join
from .recruiterflow import Recruiterflow
from .softgarden import Softgarden
from .careerpuck import CareerPuck
from .crelate import Crelate
from .fountain import Fountain
from .getro import Getro
from .manatal import Manatal
from .zoho import Zoho
from .applicantpro import ApplicantPro, ISolved
from .dover import Dover
from .jibeapply import JibeApply
from .recruitcrm import RecruitCRM
from .base import Vendor
from .pageup import PageUp
from .peopleadmin import PeopleAdmin
from .loxo import Loxo
from .simplicant import Simplicant
from .eightfold import Eightfold
from .oracle import Oracle
from .trakstar import Trakstar
from .prismhr import PrismHR
from .talentlyft import TalentLyft
from .occupop import Occupop
from .ycombinator import YCombinator
from .ukg import UKG, UltiPro
from .breezy import Breezy
from .gem import Gem
from .greenhouse import Greenhouse
from .icims import ICIMS
from .jobvite import Jobvite
from .lever import Lever
from .paylocity import Paylocity
from .personio import Personio
from .pinpoint import Pinpoint
from .recruitee import Recruitee
from .rippling import Rippling
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
        Jobvite(),
        Pinpoint(),
        Rippling(),
        ApplyToJob(),
        AgileHR(),
        CareerPlug(),
        Comeet(),
        Freshteam(),
        HiBob(),
        Homerun(),
        Join(),
        Recruiterflow(),
        Softgarden(),
        CareerPuck(),
        Crelate(),
        Fountain(),
        Getro(),
        Manatal(),
        Zoho(),
        ApplicantPro(),
        ISolved(),
        PrismHR(),
        Trakstar(),
        Oracle(),
        Eightfold(),
        Loxo(),
        Simplicant(),
        PageUp(),
        PeopleAdmin(),
        UltiPro(),
        UKG(),
        YCombinator(),
        Occupop(),
        TalentLyft(),
        Dover(),
        JibeApply(),
        RecruitCRM(),
    )
}


def classify(url: str) -> tuple[str, str] | None:
    """(vendor, board) for any URL, or None."""
    for v in VENDORS.values():
        board = v.board_from_url(url)
        if board:
            return v.name, board
    return None
