"""Cheap, rule-based labels for a posting: where it can be worked from, how
senior it is, which country it is in, and whether its company looks like a
staffing agency.

These are heuristics over short strings (a title, a location, a company name).
They are meant for filtering, not for display of facts: a label is only set
when there is a clear signal, and an unknown stays empty.
"""

from __future__ import annotations

import re

# ── workplace ───────────────────────────────────────────────────────────
_REMOTE = re.compile(
    r"(?<!\w)(remote|work(?:ing)? from home|wfh|home[- ]?based|anywhere)"
    r"(?!\w)",
    re.I,
)
_HYBRID = re.compile(r"(?<!\w)hybrid(?!\w)", re.I)
_NOT_REMOTE = re.compile(
    r"(?<!\w)(non[- ]?remote|not remote|on[- ]?site)", re.I
)


def workplace(location: str = "", title: str = "") -> str:
    """'remote', 'hybrid' or '' (not stated). Hybrid wins over remote:
    'Hybrid / Remote' means some office days."""
    text = f"{location or ''} {title or ''}"
    if _NOT_REMOTE.search(text) and not _HYBRID.search(text):
        return ""
    if _HYBRID.search(text):
        return "hybrid"
    if _REMOTE.search(text):
        return "remote"
    return ""


# ── seniority ───────────────────────────────────────────────────────────
SENIORITY = ("intern", "entry", "mid", "senior", "lead", "executive")

# (pattern, tier). First tier to match wins, checked from the most specific
# down, so 'Senior Director' is executive and 'Senior Engineer' is senior.
_TIERS = [
    (
        "intern",
        r"\b(intern|internship|co-?op|working student|werkstudent|trainee|"
        r"apprentice)\b",
    ),
    (
        "executive",
        r"\b(chief|c[- ]?level|cto|ceo|cfo|coo|cio|cmo|ciso|svp|evp|"
        r"vice president|vp|director|head of|general manager|managing "
        r"director|president)\b",
    ),
    (
        "lead",
        r"\b(lead|principal|architect|manager|team lead|distinguished|"
        r"fellow|staff (?:\w+ )?(?:engineer|developer|scientist|designer|"
        r"researcher|swe|product|program))\b",
    ),
    ("senior", r"\b(senior|sr\.?|iii|iv)\b"),
    (
        "entry",
        r"\b(junior|jr\.?|entry[- ]level|graduate|new grad|associate|"
        r"early career|i)\b",
    ),
]
_TIER_RX = [(t, re.compile(p, re.I)) for t, p in _TIERS]


def seniority(title: str = "") -> str:
    """One of SENIORITY. 'mid' means the title has no seniority word."""
    t = title or ""
    if re.match(r"\s*associate\b", t, re.I) and not _TIER_RX[1][1].search(t):
        return "entry"  # 'Associate Product Manager', but not 'Associate VP'
    for tier, rx in _TIER_RX:
        m = rx.search(t)
        if not m:
            continue
        if m.group(0).lower() in ("i", "iii", "iv") and not re.search(
            r"\b(engineer|developer|analyst|specialist|associate|"
            r"scientist|technician|accountant|nurse)\s+(i{1,3}|iv)\b",
            t,
            re.I,
        ):
            continue  # a bare roman numeral is only a level after a role word
        return tier
    return "mid"


# ── country ─────────────────────────────────────────────────────────────
_COUNTRIES = {
    "US": "united states|usa|u.s|u.s.a|us|america|united states of america",
    "GB": "united kingdom|uk|u.k|great britain|england|scotland|wales|"
    "northern ireland",
    "CA": "canada",
    "DE": "germany|deutschland",
    "FR": "france",
    "ES": "spain|españa",
    "IT": "italy|italia",
    "NL": "netherlands|the netherlands|holland|nederland",
    "BE": "belgium",
    "CH": "switzerland",
    "AT": "austria",
    "IE": "ireland",
    "PT": "portugal",
    "SE": "sweden",
    "NO": "norway",
    "DK": "denmark",
    "FI": "finland",
    "PL": "poland",
    "CZ": "czech republic|czechia",
    "RO": "romania",
    "HU": "hungary",
    "GR": "greece",
    "TR": "turkey|türkiye",
    "UA": "ukraine",
    "IL": "israel",
    "AE": "united arab emirates|uae|dubai|abu dhabi",
    "SA": "saudi arabia",
    "EG": "egypt",
    "ZA": "south africa",
    "NG": "nigeria",
    "KE": "kenya",
    "IN": "india",
    "PK": "pakistan",
    "BD": "bangladesh",
    "SG": "singapore",
    "MY": "malaysia",
    "ID": "indonesia",
    "PH": "philippines",
    "VN": "vietnam",
    "TH": "thailand",
    "JP": "japan",
    "KR": "south korea|korea",
    "CN": "china",
    "HK": "hong kong",
    "TW": "taiwan",
    "AU": "australia",
    "NZ": "new zealand",
    "BR": "brazil|brasil",
    "MX": "mexico|méxico",
    "AR": "argentina",
    "CL": "chile",
    "CO": "colombia",
    "PE": "peru",
}
_COUNTRY_NAME = {}
for _code, _names in _COUNTRIES.items():
    for _n in _names.split("|"):
        _COUNTRY_NAME[_n.lower()] = _code

_US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC"
).split()
_US_STATE_NAMES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
}
_CA_PROVINCES = "AB BC MB NB NL NS NT NU ON PE QC SK YT".split()
_CITIES = {
    "new york": "US",
    "san francisco": "US",
    "los angeles": "US",
    "seattle": "US",
    "boston": "US",
    "chicago": "US",
    "austin": "US",
    "denver": "US",
    "atlanta": "US",
    "san jose": "US",
    "san diego": "US",
    "washington dc": "US",
    "london": "GB",
    "manchester": "GB",
    "edinburgh": "GB",
    "dublin": "IE",
    "toronto": "CA",
    "vancouver": "CA",
    "montreal": "CA",
    "montréal": "CA",
    "ottawa": "CA",
    "calgary": "CA",
    "berlin": "DE",
    "munich": "DE",
    "münchen": "DE",
    "hamburg": "DE",
    "frankfurt": "DE",
    "cologne": "DE",
    "köln": "DE",
    "paris": "FR",
    "lyon": "FR",
    "madrid": "ES",
    "barcelona": "ES",
    "lisbon": "PT",
    "porto": "PT",
    "milan": "IT",
    "rome": "IT",
    "amsterdam": "NL",
    "rotterdam": "NL",
    "brussels": "BE",
    "zurich": "CH",
    "zürich": "CH",
    "geneva": "CH",
    "vienna": "AT",
    "stockholm": "SE",
    "copenhagen": "DK",
    "oslo": "NO",
    "helsinki": "FI",
    "warsaw": "PL",
    "krakow": "PL",
    "kraków": "PL",
    "prague": "CZ",
    "bucharest": "RO",
    "budapest": "HU",
    "athens": "GR",
    "istanbul": "TR",
    "tel aviv": "IL",
    "bangalore": "IN",
    "bengaluru": "IN",
    "mumbai": "IN",
    "hyderabad": "IN",
    "pune": "IN",
    "delhi": "IN",
    "gurgaon": "IN",
    "gurugram": "IN",
    "chennai": "IN",
    "singapore": "SG",
    "tokyo": "JP",
    "seoul": "KR",
    "shanghai": "CN",
    "beijing": "CN",
    "sydney": "AU",
    "melbourne": "AU",
    "auckland": "NZ",
    "são paulo": "BR",
    "sao paulo": "BR",
    "mexico city": "MX",
    "buenos aires": "AR",
    "bogotá": "CO",
    "bogota": "CO",
    "cape town": "ZA",
    "johannesburg": "ZA",
    "lagos": "NG",
    "nairobi": "KE",
}
_SPLIT = re.compile(r"[,;|/•·()\-–—]|\s{2,}")


def country(location: str = "") -> str:
    """ISO 3166-1 alpha-2 code, or '' when the location doesn't say. Looks at
    each comma/slash-separated part, from the last (usually the country) to
    the first, and recognises country names, US states and Canadian
    provinces, and well-known cities."""
    loc = (location or "").strip()
    if not loc:
        return ""
    parts = [p.strip() for p in _SPLIT.split(loc) if p.strip()]
    for p in reversed(parts):
        low = p.lower().rstrip(".")
        if low in _COUNTRY_NAME:
            return _COUNTRY_NAME[low]
    for p in reversed(parts):
        low = p.lower()
        if low in _US_STATE_NAMES and low != "georgia":
            return "US"
        if p.isupper() and len(p) == 2:
            if p in _US_STATES:
                return "US"
            if p in _CA_PROVINCES:
                return "CA"
    for p in parts:
        if p.lower() in _CITIES:
            return _CITIES[p.lower()]
    for city, code in _CITIES.items():
        if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", loc, re.I):
            return code
    return ""


# ── staffing agencies ───────────────────────────────────────────────────
# Whole-word terms in a company's name (or board slug) that mark it as a
# staffing / recruiting agency rather than the employer. Deliberately
# whole-word and specific: 'staff' or 'search' alone would catch ordinary
# companies ('Search Engine Co', 'Staffbase').
_AGENCY_TERMS = [
    r"staffing",
    r"recruit(?:ing|ment|ers?)",
    r"headhunt(?:ing|ers?)",
    r"executive search",
    r"talent (?:acquisition|solutions|partners|search)",
    r"employment (?:agency|services)",
    r"workforce solutions",
    r"personnel",
    r"manpower",
    r"temp(?:orary)? (?:agency|staff|services)",
    r"search (?:firm|partners|group|associates)",
    r"placement",
    r"randstad",
    r"adecco",
    r"robert half",
    r"kelly services",
    r"kforce",
    r"insight global",
    r"teksystems",
    r"hays",
    r"michael page",
    r"page personnel",
    r"manpowergroup",
    r"allegis",
    r"aerotek",
    r"cybercoders",
    r"jobgether",
    r"lensa",
    r"jooble",
    r"talentify",
    r"apex systems",
    r"experis",
]
_AGENCY = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(_AGENCY_TERMS) + r")(?![a-z0-9])", re.I
)


def is_agency(*names: str) -> bool:
    """True if any of the given names (company name, board slug) looks like a
    staffing agency. Hyphens and underscores count as spaces."""
    for n in names:
        text = re.sub(r"[-_.]+", " ", n or "")
        if _AGENCY.search(text):
            return True
    return False
