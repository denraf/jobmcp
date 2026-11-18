"""Pay found in a posting: a range or a single figure, with its currency and
the period it was quoted for.

Most postings state their pay in the description (pay-transparency laws made
that the norm in much of the US, and common elsewhere), so this reads the
text. It is built to prefer a missing salary over a wrong one: a figure counts
only when the words around it say it is pay (salary, range, compensation,
"per year" ...) and not a bonus, funding round, benefit or revenue. Open-ended
forms ("up to", "from") are ignored, because a range needs both ends.

Amounts are stored per year, in the posting's own currency, plus an
approximate USD equivalent used only to compare postings across currencies
(the salary bands of the jobs page). The rates are rough on purpose; they only
decide which band a posting is listed under.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class Salary(NamedTuple):
    low: int  # per year, in `currency`
    high: int
    currency: str  # ISO 4217
    period: str  # as quoted: year | month | week | day | hour


HOURS_PER_YEAR = 2080
PERIODS = {
    "year": 1,
    "month": 12,
    "week": 52,
    "day": 260,
    "hour": HOURS_PER_YEAR,
}

# What one unit of the currency is worth in US dollars. Approximate: they
# place a posting in a band, they are never shown.
USD_RATE = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.73,
    "AUD": 0.65,
    "NZD": 0.60,
    "CHF": 1.12,
    "SGD": 0.74,
    "SEK": 0.095,
    "NOK": 0.093,
    "DKK": 0.145,
    "PLN": 0.25,
    "INR": 0.012,
    "JPY": 0.0066,
}
# A yearly figure outside these is not a salary (a postal code, a headcount, a
# budget). In the currency's own units.
BOUNDS = {
    "INR": (150_000, 100_000_000),
    "JPY": (1_000_000, 100_000_000),
    "SEK": (100_000, 10_000_000),
    "NOK": (100_000, 10_000_000),
    "DKK": (100_000, 10_000_000),
    "PLN": (20_000, 3_000_000),
}
DEFAULT_BOUNDS = (8_000, 2_000_000)

# Bands for the filter, in approximate USD per year: (from, up to but not
# including). A posting is in a band when its range overlaps it.
BANDS: dict[str, tuple[int, int | None]] = {
    "under-50k": (0, 50_000),
    "50k-75k": (50_000, 75_000),
    "75k-100k": (75_000, 100_000),
    "100k-150k": (100_000, 150_000),
    "150k-200k": (150_000, 200_000),
    "200k-plus": (200_000, None),
}

_SYMBOL = {
    "US$": "USD", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
    "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD", "NZ$": "NZD",
    "S$": "SGD",
}  # fmt: skip
_CODES = "USD|EUR|GBP|CAD|AUD|NZD|CHF|SGD|SEK|NOK|DKK|PLN|INR|JPY"
_CUR = (
    r"(?:US\$|CA\$|AU\$|NZ\$|C\$|A\$|S\$|[$€£¥₹]|" + _CODES + r")(?![A-Za-z])"
)
_NUM = (
    r"\d{1,3}(?:[.,   ]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?"
)


def _amount(n: str) -> str:
    return rf"(?P<{n}>{_NUM})\s?(?P<{n}k>[kK])?(?![\w])"


_DASH = r"\s*(?:-|–|—|‒|to|and|bis)\s*"
_RANGE = re.compile(
    rf"(?P<c1>{_CUR})?\s?{_amount('a')}{_DASH}(?P<c2>{_CUR})?\s?{_amount('b')}",
    re.I,
)
_SINGLE = re.compile(rf"(?P<c1>{_CUR})\s?{_amount('a')}", re.I)
_CODE_AFTER = re.compile(rf"^\s*\(?({_CODES})\b", re.I)

# words that say a figure is pay
_PAY_BEFORE = re.compile(
    r"\b(?:salary|salaries|pay|paid|compensation|wage|remuneration|base|"
    r"range|ote|earn(?:ings)?|rate|annual|hourly|gross|package|"
    # German, French, Spanish, Italian, Dutch, Scandinavian
    r"gehalt|vergütung|brutto|salaire|rémunération|salario|retribución|"
    r"stipendio|retribuzione|salaris|loon|lön|løn)\b",
    re.I,
)
# words that say it is something else
_NOT_PAY_BEFORE = re.compile(
    r"bonus|sign[- ]?on|signing|relocation|stipend|allowance|reimburse|"
    r"tuition|funding|raised|revenue|valuation|equity|grant|budget|insurance|"
    r"deductible|donat|401|retirement",
    re.I,
)
# a lone figure after these is one end of an open range, which is not stated
_OPEN_BEFORE = re.compile(
    r"(?:up\s+to|starting\s+(?:at|from)|from|minimum|min\.?|over|under|"
    r"at\s+least|above|below)\s*$",
    re.I,
)
_NOT_PAY_AFTER = re.compile(
    r"^\s*(?:million|billion|mm\b|bn\b|m\b|b\b|(?:in|of)\s+"
    r"(?:funding|revenue|sales|bonus))",
    re.I,
)
_PERIOD_AFTER = [
    ("hour", re.compile(r"^\W{0,3}(?:pro\s+stunde|par\s+heure|per\s+hour|an\s+hour|/\s*h(?:ou)?r\b|/h\b|hourly|p\.?h\.?\b|per\s+hr)", re.I)),  # noqa: E501
    ("year", re.compile(r"^\W{0,3}(?:pro\s+jahr|jährlich|par\s+an|por\s+año|per\s+jaar|per\s+year|a\s+year|/\s*y(?:ea)?r\b|annually|annual|per\s+annum|p\.?a\.?\b|yearly|per\s+yr)", re.I)),  # noqa: E501
    ("month", re.compile(r"^\W{0,3}(?:pro\s+monat|monatlich|par\s+mois|al\s+mes|per\s+maand|per\s+month|a\s+month|/\s*mo(?:nth)?\b|monthly)", re.I)),  # noqa: E501
    ("week", re.compile(r"^\W{0,3}(?:per\s+week|a\s+week|/\s*w(?:ee)?k\b|weekly)", re.I)),  # noqa: E501
    ("day", re.compile(r"^\W{0,3}(?:per\s+day|a\s+day|/\s*day\b|daily)", re.I)),  # noqa: E501
]  # fmt: skip
_PERIOD_BEFORE = [
    ("hour", re.compile(r"hourly|per\s+hour|hourly\s+rate|\bwage", re.I)),
    ("year", re.compile(r"annual|yearly|per\s+year|salary|salaries|base\s+pay", re.I)),  # noqa: E501
    ("month", re.compile(r"monthly|per\s+month", re.I)),
]  # fmt: skip
_QUICK = re.compile(rf"(?:{_CUR})\s?\d|\d[kK]?\s?(?:{_CODES}\b|[€£])", re.I)
_LEADING_CODE = re.compile(rf"^\s*\(?(?:{_CODES}|[€£])\)?", re.I)


def _number(s: str) -> float | None:
    """'120,000' '120.000' '85,50' '1 200' -> a number; None if unreadable."""
    s = re.sub(r"[   ]", "", s)
    if "," in s and "." in s:  # the last one is the decimal point
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thousands = "." if dec == "," else ","
        s = s.replace(thousands, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        head, _, tail = s.rpartition(sep)
        if len(tail) == 3 and head.replace(sep, "").isdigit():  # 120,000
            s = head.replace(sep, "") + tail
        else:  # 85,50 / 85.50
            s = head.replace(sep, "") + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def _currency(c1: str | None, c2: str | None, after: str) -> str | None:
    for c in (c1, c2):
        if c:
            c = c.upper()
            return _SYMBOL.get(c, c if c in USD_RATE else None)
    m = _CODE_AFTER.match(after)
    if m:
        return m.group(1).upper()
    m = re.match(r"\s?([€£])", after)  # '45 000 - 55 000 €'
    return _SYMBOL[m.group(1)] if m else None


def _has_period(after: str) -> bool:
    return any(rx.search(after) for _, rx in _PERIOD_AFTER)


def _period(before: str, after: str, value: float, kilo: bool) -> str | None:
    for name, rx in _PERIOD_AFTER:
        if rx.search(after):
            return name
    for name, rx in _PERIOD_BEFORE:
        if rx.search(before[-40:]):
            return name
    # no period word: a figure with k, or in the tens of thousands, is a year
    return "year" if kilo or value >= 15_000 else None


def _candidates(text: str):
    """Every range, then every single figure not already inside one."""
    taken: list[tuple[int, int]] = []
    for m in _RANGE.finditer(text):
        taken.append((m.start(), m.end()))
        yield m, True
    for m in _SINGLE.finditer(text):
        if not any(a <= m.start() < b for a, b in taken):
            yield m, False


def parse(text: str) -> Salary | None:
    """The pay a posting states, or None."""
    if not text or not _QUICK.search(text):
        return None
    found: list[Salary] = []
    for m, is_range in _candidates(text):
        g = m.groupdict()
        before = text[max(0, m.start() - 90) : m.start()]
        near, after = before[-30:], text[m.end() : m.end() + 60]
        a = _number(g["a"])
        b = _number(g["b"]) if is_range else a
        if a is None or b is None:
            continue
        ka, kb = bool(g.get("ak")), bool(g.get("bk")) if is_range else False
        if is_range:  # '120-150k' and '$120k-150' share the k
            if kb and not ka and a < 1000:
                ka = True
            if ka and not kb and b < 1000:
                kb = True
        a, b = (
            a * 1000 if ka else a,
            b * 1000 if kb else (a if not is_range else b),
        )
        if not is_range:
            b = a
        if _NOT_PAY_AFTER.search(after) or _NOT_PAY_BEFORE.search(near):
            continue
        if not is_range and (
            _OPEN_BEFORE.search(before) or after.lstrip().startswith("+")
        ):
            continue  # "up to $150k", "$85k+": one end only
        cur = _currency(g.get("c1"), g.get("c2"), after)
        if cur is None:
            continue
        if cur == "USD" and re.match(r"\s?(?:CAD|AUD|NZD|SGD)\b", after, re.I):
            cur = after.strip().upper()[:3]  # '$90,000 CAD'
        tail = _LEADING_CODE.sub("", after)  # 'USD per year' -> ' per year'
        period = _period(before, tail, max(a, b), ka or kb)
        if period is None or a > b or a <= 0:
            continue
        if not (
            _has_period(tail)
            or _PAY_BEFORE.search(before)
            or _PAY_BEFORE.search(tail[:25])
        ):
            continue  # nothing says this is pay
        low, high = round(a * PERIODS[period]), round(b * PERIODS[period])
        lo_ok, hi_ok = BOUNDS.get(cur, DEFAULT_BOUNDS)
        if not (lo_ok <= low and high <= hi_ok) or high > 5 * low:
            continue
        found.append(Salary(low, high, cur, period))
    if not found:
        return None
    first = found[0]
    # Pay quoted per location tier or per level: the whole span, as long as it
    # is the same currency and still one believable range.
    same = [x for x in found if x.currency == first.currency]
    low, high = min(x.low for x in same), max(x.high for x in same)
    if high > 3 * low:
        low, high = first.low, first.high
    return Salary(low, high, first.currency, first.period)


def usd(amount: int, currency: str) -> int:
    return round(amount * USD_RATE.get(currency, 1.0))


def usd_range(s: Salary) -> tuple[int, int]:
    return usd(s.low, s.currency), usd(s.high, s.currency)


def band_keys(v) -> list[str]:
    """The band names in a filter given as a list or as 'a,b'; raises
    ValueError on a name that is not a band."""
    items = v.split(",") if isinstance(v, str) else list(v or [])
    out = [
        x.strip().lower() for x in items if isinstance(x, str) and x.strip()
    ]
    bad = [x for x in out if x not in BANDS]
    if bad:
        raise ValueError(
            f"unknown salary band {bad}; use any of {list(BANDS)}"
        )
    return out


# ── display ─────────────────────────────────────────────────────────────
_PREFIX = {"USD": "$", "EUR": "€", "GBP": "£"}
_SUFFIX = {
    "year": "",
    "month": "/mo",
    "week": "/wk",
    "day": "/day",
    "hour": "/hr",
}


def _short(v: float, period: str) -> str:
    if period != "year":
        return f"{v:,.0f}" if round(v, 2) == round(v) else f"{v:,.2f}"
    if v >= 1000 and v % 1000 == 0:
        return f"{int(v // 1000)}k"
    if v >= 1000 and v % 100 == 0:
        return f"{v / 1000:.1f}k"
    return f"{int(v):,}"


def format_salary(
    low: int | None, high: int | None, currency: str, period: str
) -> str:
    """'$120k–150k', '€55k–65k', '$45–60/hr', 'CAD 90k–110k'. '' when there is
    no salary. Amounts are stored per year and shown in the period quoted."""
    if not low or not high or not currency:
        return ""
    per = PERIODS.get(period or "year", 1)
    a, b = low / per, high / per
    pre = _PREFIX.get(currency, currency + " ")
    suffix = _SUFFIX.get(period or "year", "")
    if round(a, 2) == round(b, 2):
        return f"{pre}{_short(a, period)}{suffix}"
    return f"{pre}{_short(a, period)}–{_short(b, period)}{suffix}"


def columns(text: str) -> dict:
    """The jobs-table values for the pay in `text` (all empty when it states
    none)."""
    s = parse(text)
    if not s:
        return {
            "salary_min": None,
            "salary_max": None,
            "salary_currency": "",
            "salary_period": "",
            "salary_usd_min": None,
            "salary_usd_max": None,
        }
    lo, hi = usd_range(s)
    return {
        "salary_min": s.low,
        "salary_max": s.high,
        "salary_currency": s.currency,
        "salary_period": s.period,
        "salary_usd_min": lo,
        "salary_usd_max": hi,
    }


_UNITS = {
    "YEAR": "year",
    "ANNUAL": "year",
    "MONTH": "month",
    "WEEK": "week",
    "DAY": "day",
    "HOUR": "hour",
}


def from_schema_org(base_salary) -> str:
    """A schema.org `baseSalary` (MonetaryAmount) as a sentence the parser
    reads back, or ''. Many career sites publish pay only there."""
    if not isinstance(base_salary, dict):
        return ""
    value = base_salary.get("value")
    value = value if isinstance(value, dict) else {}
    cur = str(base_salary.get("currency") or value.get("currency") or "")
    unit = _UNITS.get(str(value.get("unitText") or "").strip().upper())

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    lo, hi = num(value.get("minValue")), num(value.get("maxValue"))
    one = num(value.get("value"))
    if lo is None and hi is None:
        lo = hi = one
    lo, hi = (lo if lo is not None else hi), (hi if hi is not None else lo)
    return sentence(cur, lo, hi, unit)


def sentence(currency: str, low, high, unit: str | None) -> str:
    """'Salary: USD 120,000 - 150,000 per year', which `parse` reads back;
    '' when the figures are not a believable pay range. Vendors whose API
    gives pay as fields use this to put it in the text."""
    cur = (currency or "").upper()
    if not (
        cur in USD_RATE and unit in PERIODS and low and high and low <= high
    ):
        return ""
    return f"Salary: {cur} {low:,.0f} - {high:,.0f} per {unit}"


def from_ashby(compensation) -> str:
    """The salary component of an Ashby posting's `compensation`, as a
    sentence (see `sentence`), or ''."""
    if not isinstance(compensation, dict):
        return ""
    for comp in compensation.get("summaryComponents") or []:
        if comp.get("compensationType") != "Salary":
            continue
        unit = _UNITS.get(
            str(comp.get("interval") or "").split()[-1].upper()
            if comp.get("interval")
            else ""
        )
        return sentence(
            comp.get("currencyCode") or "",
            comp.get("minValue"),
            comp.get("maxValue") or comp.get("minValue"),
            unit,
        )
    return ""
