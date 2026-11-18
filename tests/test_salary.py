"""Pay found in postings: the extractor, the display, what the sync stores, the
band filter, and the API, MCP and migration that carry it."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from jobmcp import db, migrations, salary
from jobmcp.api import app
from jobmcp.orm import Job
from jobmcp.services import ServiceError, search, sync

from .helpers import fj, mk_board, mk_user


def shown(text: str) -> str | None:
    s = salary.parse(text)
    return (
        salary.format_salary(s.low, s.high, s.currency, s.period)
        if s
        else None
    )


# ── the extractor ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [
        ("The salary range is $120,000 - $150,000 per year.", "$120k–150k"),
        ("Pay range: $120k–$150k", "$120k–150k"),
        ("Compensation: 120-150k USD", "$120k–150k"),
        ("Base salary between $95,000 and $115,000 annually", "$95k–115k"),
        ("Salary $120,000/year", "$120k"),
        ("You will earn $95,000 a year", "$95k"),
        ("Annual salary of $98,500", "$98.5k"),
        ("USD 100,000–120,000 per year", "$100k–120k"),
        (
            "Salary range from $100k to $120k depending on experience",
            "$100k–120k",
        ),
        ("OTE $150k-$200k", "$150k–200k"),
        ("Salary: $85,000 - $95,000+", "$85k–95k"),
        # other currencies and the way each country writes numbers
        ("Salary: €55.000 - €65.000 per year", "€55k–65k"),
        ("Salary: £45,000 to £55,000 p.a.", "£45k–55k"),
        ("Pay: CAD 90,000 - 110,000", "CAD 90k–110k"),
        ("The range is $90,000 - $110,000 CAD per year", "CAD 90k–110k"),
        ("Gehalt: 60.000 - 75.000 EUR", "€60k–75k"),
        ("Salaire: 45 000 - 55 000 € par an", "€45k–55k"),
        # other periods are shown as quoted
        ("Hourly rate: $45 - $60 per hour", "$45–60/hr"),
        ("We pay between $18 and $24 an hour.", "$18–24/hr"),
        ("$22.50/hr to $28.00/hr", "$22.50–28/hr"),
        ("Pay: $1,200 - $1,500 per week", "$1,200–1,500/wk"),
        ("Brutto 4.500 - 5.500 EUR pro Monat", "€4,500–5,500/mo"),
        # one range quoted per location: the whole span
        (
            "Zone A: $130,000 - $160,000 annual salary. "
            "Zone B: $120,000 - $150,000 annual salary.",
            "$120k–160k",
        ),
        # money in the same text that is not the salary
        (
            "Salary range $180,000 - $200,000 plus $20,000 relocation",
            "$180k–200k",
        ),
        (
            "Experience: 3 - 5 years. Base salary $100,000 - $120,000.",
            "$100k–120k",
        ),
    ],
)
def test_pay_in_the_text(text, expected):
    assert shown(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "We are hiring a data engineer to join the team.",
        "We raised $50 million in funding. Join us!",
        "Our revenue grew from $10 to $20 million",
        "Sign-on bonus of $10,000 - $15,000 for this role",
        "Competitive salary. 401k match up to $5,000",
        "Annual tuition reimbursement of $5,250",
        "Equity: $20,000-$40,000 stock grant",
        "Budget of $200,000 - $300,000 for the project",
        "Salary up to $150,000",  # one end only
        "Salary: $85,000+",
        "Starting at $90,000",
        "Salary: $5 - $10",  # not a believable yearly figure
        "Phone 555-1234, zip 90210 - 90211",
        "The company has 2,000-3,000 employees",
        "Equity worth $120,000 - $150,000 per year vested",
        "$100,000 - $500,000 in sales",
    ],
)
def test_money_that_is_not_the_salary_is_ignored(text):
    assert salary.parse(text) is None


def test_hourly_pay_is_stored_per_year_and_shown_as_quoted():
    s = salary.parse("Pay: $45 - $60 per hour")
    assert (s.low, s.high, s.period) == (93_600, 124_800, "hour")
    assert salary.format_salary(s.low, s.high, "USD", "hour") == "$45–60/hr"


def test_other_currencies_are_placed_by_an_approximate_dollar_range():
    s = salary.parse("Salary: £45,000 to £55,000 p.a.")
    lo, hi = salary.usd_range(s)
    assert (lo, hi) == (57_150, 69_850)


def test_a_missing_salary_is_blank_not_zero():
    assert salary.format_salary(None, None, "", "") == ""
    assert salary.columns("no pay here")["salary_min"] is None


def test_structured_pay_is_read_back():
    pay = salary.from_schema_org(
        {
            "currency": "USD",
            "value": {
                "minValue": 120000,
                "maxValue": 150000,
                "unitText": "YEAR",
            },
        }
    )
    assert salary.parse(pay) == salary.Salary(120000, 150000, "USD", "year")
    assert salary.from_schema_org({"currency": "USD", "value": {}}) == ""
    assert salary.from_schema_org("nonsense") == ""


def test_bands_name_rejects_unknown_ones():
    assert salary.band_keys("100k-150k, 200k-plus") == [
        "100k-150k",
        "200k-plus",
    ]
    with pytest.raises(ValueError):
        salary.band_keys("rich")


def test_a_pathological_text_is_fast():
    import time

    text = ("$1,2" * 4000) + " - " + ("9" * 5000)
    t = time.time()
    salary.parse(text)
    assert time.time() - t < 2


# ── what the sync stores, and the filter ───────────────────────────────
@pytest.fixture()
def jobs(fresh_db):
    b = mk_board("greenhouse", "acme")
    sync.sync_board(
        b,
        lambda v, s: [
            fj("Low", desc="Salary: $40,000 - $48,000 per year."),
            fj("Mid", desc="Pay range $90,000 - $110,000 per year."),
            fj("High", desc="Salary range: $180,000 - $240,000."),
            fj("Hourly", desc="Pay: $40 - $50 per hour."),
            fj("Euro", desc="Salary: €80.000 - €95.000 per year"),
            fj("Silent", desc="A great place to work."),
        ],
    )


def band(*names):
    with db.session() as s:
        r = search.search_jobs(
            s, None, None, None, browse=True, salary=list(names)
        )
        return sorted(h.job.title for h in r["hits"])


def test_the_sync_stores_the_pay_with_the_posting(jobs):
    with db.session() as s:
        rows = {j.title: j for j in s.scalars(select(Job))}
    mid = rows["Mid"]
    assert (mid.salary_min, mid.salary_max, mid.salary_currency) == (
        90000,
        110000,
        "USD",
    )
    assert mid.salary_period == "year" and mid.salary_usd_min == 90000
    assert rows["Hourly"].salary_period == "hour"
    assert rows["Euro"].salary_currency == "EUR"
    assert rows["Euro"].salary_usd_min == round(80000 * 1.08)
    assert (
        rows["Silent"].salary_min is None
        and rows["Silent"].salary_period == ""
    )


def test_a_new_description_replaces_the_pay_and_a_missing_one_keeps_it(jobs):
    b = mk_board("greenhouse", "other")
    sync.sync_board(
        b,
        lambda v, s: [
            fj("Role", board="other", desc="Salary: $70,000 - $80,000")
        ],
    )
    sync.sync_board(b, lambda v, s: [fj("Role", board="other", desc="")])
    with db.session() as s:
        j = s.scalar(select(Job).where(Job.title == "Role"))
        assert (
            j.salary_min == 70000
        )  # this fetch had no text: keep what we know
    sync.sync_board(
        b, lambda v, s: [fj("Role", board="other", desc="Now unpaid.")]
    )
    with db.session() as s:
        assert (
            s.scalar(select(Job).where(Job.title == "Role")).salary_min is None
        )


def test_a_band_keeps_postings_whose_pay_overlaps_it(jobs):
    assert band("under-50k") == ["Low"]
    # 90k-110k, $40-50 an hour (83k-104k) and 80k-95k euro (86k-103k) all
    # reach into it
    assert band("75k-100k") == ["Euro", "Hourly", "Mid"]
    assert band("100k-150k") == ["Euro", "Hourly", "Mid"]
    assert band("200k-plus") == ["High"]
    assert band("50k-75k") == []


def test_a_band_edge_belongs_to_the_band_above(jobs):
    # Mid is 90k-110k: it overlaps 75k-100k and 100k-150k, not 150k-200k
    assert "Mid" not in band("150k-200k")
    assert "High" in band("150k-200k")  # 180k-240k


def test_several_bands_and_the_postings_that_state_no_pay(jobs):
    assert band("under-50k", "200k-plus") == ["High", "Low"]
    assert "Silent" not in band(
        "under-50k",
        "50k-75k",
        "75k-100k",
        "100k-150k",
        "150k-200k",
        "200k-plus",
    )


def test_an_unknown_band_is_refused(jobs):
    with db.session() as s, pytest.raises(ServiceError) as e:
        search.search_jobs(s, None, None, None, browse=True, salary=["rich"])
    assert "unknown salary band" in str(e.value)


# ── API and MCP ─────────────────────────────────────────────────────────
@pytest.fixture()
def c(jobs):
    return TestClient(app)


def test_the_public_list_shows_the_pay_and_filters_by_band(c):
    hits = {h["title"]: h for h in c.get("/public/jobs").json()["hits"]}
    assert hits["Mid"]["salary"] == "$90k–110k"
    assert hits["Hourly"]["salary"] == "$40–50/hr"
    assert hits["Euro"]["salary"] == "€80k–95k"
    assert (
        hits["Silent"]["salary"] == "" and hits["Silent"]["salary_min"] is None
    )
    assert (hits["Mid"]["salary_currency"], hits["Mid"]["salary_period"]) == (
        "USD",
        "year",
    )
    got = c.get("/public/jobs?salary=200k-plus").json()
    assert [h["title"] for h in got["hits"]] == ["High"] and got["total"] == 1
    both = c.get("/public/jobs?salary=under-50k,200k-plus").json()
    assert sorted(h["title"] for h in both["hits"]) == ["High", "Low"]
    assert c.get("/public/jobs?salary=rich").status_code == 400


def test_the_band_is_part_of_the_cache_key(c):
    first = c.get("/public/jobs?salary=under-50k").json()["total"]
    second = c.get("/public/jobs?salary=200k-plus").json()["total"]
    assert (first, second) == (1, 1)
    assert c.get("/public/jobs").json()["total"] == 6


def test_the_authenticated_search_filters_by_band_too(c):
    mk_user("a@x.io")
    token = c.post(
        "/auth/login",
        json={"email": "a@x.io", "password": "correct-horse-battery"},
    ).json()["token"]
    r = c.get(
        "/jobs/search?title=Mid&salary=75k-100k",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert [h["title"] for h in r["hits"]] == ["Mid"]
    assert r["hits"][0]["salary"] == "$90k–110k"


def test_the_agent_tool_reports_the_pay_and_takes_a_band(jobs):
    from jobmcp import mcp_server

    with db.session() as s:
        r = search.search_jobs(
            s,
            mk_user("agent@x.io"),
            None,
            None,
            browse=True,
            salary="150k-200k",
        )
        out = mcp_server._job(r["hits"][0].job)
    assert out["title"] == "High" and out["salary"] == "$180k–240k"
    assert (out["salary_min"], out["salary_currency"]) == (180000, "USD")
    # and the tool itself declares the parameter
    import inspect as _i

    assert "salary_band" in _i.signature(mcp_server.search_jobs).parameters


# ── the migration ───────────────────────────────────────────────────────
def test_migration_adds_the_columns_and_reads_the_pay_of_existing_postings(
    jobs,
):
    eng = db.engine()
    with eng.begin() as conn:
        conn.execute(text("DROP INDEX ix_jobs_browse ON jobs"))
        conn.execute(text("DROP INDEX ix_jobs_active_salary ON jobs"))
        for col, _ in migrations.SALARY_COLUMNS:
            conn.execute(text(f"ALTER TABLE jobs DROP COLUMN {col}"))
    assert not migrations.is_current()
    changed = migrations.ensure_schema()
    assert any("salary" in x for x in changed) and migrations.is_current()
    names = {
        i["name"]: i["column_names"] for i in inspect(eng).get_indexes("jobs")
    }
    assert "ix_jobs_active_salary" in names
    assert names["ix_jobs_browse"][-2:] == ["salary_usd_min", "salary_usd_max"]
    with db.session() as s:
        rows = {j.title: j for j in s.scalars(select(Job))}
    assert (rows["Mid"].salary_min, rows["Mid"].salary_max) == (90000, 110000)
    assert rows["Silent"].salary_min is None
    assert migrations.ensure_schema() == []  # and it is a one-off


def test_an_older_browse_index_is_rebuilt_with_the_salary_columns(jobs):
    eng = db.engine()
    with eng.begin() as conn:
        conn.execute(text("DROP INDEX ix_jobs_browse ON jobs"))
        conn.execute(
            text(
                "CREATE INDEX ix_jobs_browse ON jobs (active, sort_at, id, "
                "board_id, workplace, seniority, country)"
            )
        )
    assert any("ix_jobs_browse" in x for x in migrations.ensure_schema())
    cols = {
        i["name"]: i["column_names"] for i in inspect(eng).get_indexes("jobs")
    }
    assert "salary_usd_max" in cols["ix_jobs_browse"]


# ── pay that vendors give as fields ─────────────────────────────────────
def test_ashby_pay_given_only_as_a_field_is_put_in_the_text(monkeypatch):
    from jobmcp import http
    from jobmcp.vendors import VENDORS

    from .test_vendors import FakeResp

    seen = {}

    def get(url, **kw):
        seen.update(kw)
        return FakeResp(
            {
                "jobs": [
                    {
                        "title": " Engineer ",
                        "jobUrl": "https://x/1",
                        "descriptionPlain": "Build things.",
                        "compensation": {
                            "summaryComponents": [
                                {
                                    "compensationType": "EquityPercentage",
                                    "interval": "NONE",
                                },
                                {
                                    "compensationType": "Salary",
                                    "interval": "1 YEAR",
                                    "currencyCode": "USD",
                                    "minValue": 211400,
                                    "maxValue": 290600,
                                },
                            ]
                        },
                    },
                    {
                        "title": "Already says it",
                        "jobUrl": "https://x/2",
                        "descriptionPlain": (
                            "Salary: $90,000 - $100,000 per year."
                        ),
                        "compensation": {
                            "summaryComponents": [
                                {
                                    "compensationType": "Salary",
                                    "interval": "1 YEAR",
                                    "currencyCode": "USD",
                                    "minValue": 1,
                                    "maxValue": 2,
                                }
                            ]
                        },
                    },
                ]
            }
        )

    monkeypatch.setattr(http, "get", get)
    a, b = VENDORS["ashby"].fetch("ramp")
    assert seen["params"] == {"includeCompensation": "true"}
    assert shown(a.description) == "$211.4k–290.6k"
    assert a.description.startswith("Build things.")
    assert shown(b.description) == "$90k–100k"  # the text wins over the field


def test_ashby_hourly_and_missing_components():
    assert (
        salary.parse(
            salary.from_ashby(
                {
                    "summaryComponents": [
                        {
                            "compensationType": "Salary",
                            "interval": "1 HOUR",
                            "currencyCode": "USD",
                            "minValue": 40,
                            "maxValue": 50,
                        }
                    ]
                }
            )
        ).period
        == "hour"
    )
    assert salary.from_ashby({"summaryComponents": []}) == ""
    assert salary.from_ashby(None) == ""


def test_a_posting_page_that_only_gives_baseSalary_still_has_its_pay():
    from jobmcp.vendors.detail import json_ld_posting, ld_fields

    page = (
        '<script type="application/ld+json">{"@type": "JobPosting", '
        '"title": "Cook", "description": "<p>Make food.</p>", '
        '"baseSalary": {"@type": "MonetaryAmount", "currency": "USD", '
        '"value": {"@type": "QuantitativeValue", "minValue": 18, '
        '"maxValue": 22, "unitText": "HOUR"}}}</script>'
    )
    f = ld_fields(json_ld_posting(page))
    assert f["description"].startswith("Make food.")
    assert shown(f["description"]) == "$18–22/hr"
    # a page whose text already says it is left alone
    said = page.replace("Make food.", "Pay: $20 per hour")
    assert "Salary:" not in ld_fields(json_ld_posting(said))["description"]
