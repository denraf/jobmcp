import pytest

from jobmcp.vendors import VENDORS, classify
from jobmcp.vendors.workday import _posted
from datetime import date


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://boards.greenhouse.io/acme/jobs/123?gh_src=x",
            ("greenhouse", "acme"),
        ),
        (
            "https://job-boards.greenhouse.io/Acme/jobs/123",
            ("greenhouse", "acme"),
        ),
        (
            "https://boards.greenhouse.io/embed/job_board?for=Acme",
            ("greenhouse", "acme"),
        ),
        ("https://boards.greenhouse.io/embed/job_app?token=1", None),
        ("https://boards.greenhouse.io/robots.txt", None),
        (
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            ("greenhouse", "acme"),
        ),
        ("https://jobs.lever.co/acme/abc-123", ("lever", "acme")),
        ("https://jobs.eu.lever.co/acme", ("lever", "eu/acme")),
        ("https://jobs.ashbyhq.com/Acme/uuid", ("ashby", "Acme")),
        ("https://apply.workable.com/acme/j/ABC123/", ("workable", "acme")),
        (
            "https://apply.workable.com/api/v1/widget/accounts/acme",
            ("workable", "acme"),
        ),
        (
            "https://jobs.smartrecruiters.com/LinkedIn3/123-title",
            ("smartrecruiters", "LinkedIn3"),
        ),
        ("https://acme.recruitee.com/o/engineer", ("recruitee", "acme")),
        ("https://www.recruitee.com/pricing", None),
        ("https://acme.bamboohr.com/careers/12", ("bamboohr", "acme")),
        ("https://acme.jobs.personio.de/job/1", ("personio", "acme.de")),
        ("https://acme.teamtailor.com/jobs/1-x", ("teamtailor", "acme")),
        ("https://acme.breezy.hr/p/abc-title", ("breezy", "acme")),
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x",
            ("workday", "nvidia.wd5/NVIDIAExternalCareerSite"),
        ),
        ("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/x/jobs", None),
        ("https://jobs.gem.com/fetch", ("gem", "fetch")),
        (
            "https://jobs.gem.com/Function-Health/am9icG9zdDoW",
            ("gem", "function-health"),
        ),
        ("https://jobs.gem.com/api/public/graphql", None),
        ("https://www.gem.com/product", None),
        ("https://example.com/careers", None),
    ],
)
def test_classify(url, expected):
    assert classify(url) == expected


def test_workday_posted():
    t = date(2026, 11, 10)
    assert _posted("Posted Today", t) == "2026-11-10"
    assert _posted("Posted 3 Days Ago", t) == "2026-11-07"
    assert _posted(None, t) == ""


class FakeResp:
    def __init__(self, data=None, content=b""):
        self._d, self.content, self.status_code = data, content, 200
        self.headers = {"content-type": "application/json"}
        self.text = content.decode() if content else ""

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def patch(monkeypatch, resp):
    from jobmcp import http

    monkeypatch.setattr(http, "get", lambda *a, **k: resp)
    monkeypatch.setattr(http, "post", lambda *a, **k: resp)


def test_greenhouse_parse(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            {
                "jobs": [
                    {
                        "title": " Data Engineer ",
                        "absolute_url": "https://x/1",
                        "company_name": "Acme",
                        "location": {"name": "Remote"},
                        "first_published": "2026-10-01",
                        "content": (
                            "&lt;p&gt;Build &lt;b&gt;pipes&lt;/b&gt;&lt;/p&gt;"
                        ),
                    }
                ]
            }
        ),
    )
    [j] = VENDORS["greenhouse"].fetch("acme")
    assert (j.title, j.company, j.location, j.description) == (
        "Data Engineer",
        "Acme",
        "Remote",
        "Build pipes",
    )


def test_lever_parse(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            [
                {
                    "text": "SRE",
                    "hostedUrl": "https://x",
                    "createdAt": 1760000000000,
                    "categories": {"location": "Berlin"},
                    "descriptionPlain": "hi",
                }
            ]
        ),
    )
    [j] = VENDORS["lever"].fetch("acme")
    assert j.location == "Berlin" and j.posted_at.startswith("2025-10")


def test_ashby_skips_unlisted(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            {
                "jobs": [
                    {"title": " A", "isListed": True, "jobUrl": "u"},
                    {"title": "B", "isListed": False},
                ]
            }
        ),
    )
    assert [j.title for j in VENDORS["ashby"].fetch("acme")] == ["A"]


def test_workable_parse(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            {
                "name": "Acme",
                "jobs": [
                    {
                        "title": "PM",
                        "city": "Paris",
                        "country": "France",
                        "url": "u",
                        "published_on": "2026-10-02",
                    }
                ],
            }
        ),
    )
    [j] = VENDORS["workable"].fetch("acme")
    assert j.company == "Acme" and j.location == "Paris, France"


def test_smartrecruiters_paginates(monkeypatch):
    pages = [
        FakeResp(
            {
                "totalFound": 101,
                "content": [
                    {
                        "id": str(i),
                        "name": "n",
                        "location": {
                            "city": "X",
                            "country": "us",
                            "remote": True,
                        },
                    }
                    for i in range(100)
                ],
            }
        ),
        FakeResp(
            {
                "totalFound": 101,
                "content": [
                    {"id": "z", "name": "last", "company": {"name": "Co"}}
                ],
            }
        ),
    ]
    from jobmcp import http

    monkeypatch.setattr(http, "get", lambda *a, **k: pages.pop(0))
    jobs = VENDORS["smartrecruiters"].fetch("Co")
    assert len(jobs) == 101 and jobs[0].location == "Remote - X, US"


def test_recruitee_bamboo_breezy(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            {
                "offers": [
                    {
                        "title": "T",
                        "careers_url": "u",
                        "city": "A",
                        "country": "B",
                        "status": "published",
                    },
                    {"title": "draft", "status": "draft"},
                ]
            }
        ),
    )
    assert [j.title for j in VENDORS["recruitee"].fetch("acme")] == ["T"]
    patch(
        monkeypatch,
        FakeResp(
            {
                "result": [
                    {
                        "id": "7",
                        "jobOpeningName": "Dev",
                        "location": {"city": "Boise", "state": "ID"},
                        "isRemote": True,
                    }
                ]
            }
        ),
    )
    [j] = VENDORS["bamboohr"].fetch("acme")
    assert j.url.endswith("/careers/7") and j.location == "Remote - Boise, ID"
    patch(
        monkeypatch,
        FakeResp(
            [
                {
                    "name": "QA",
                    "url": "u",
                    "location": {"name": "NYC"},
                    "company": {"name": "Acme"},
                }
            ]
        ),
    )
    assert VENDORS["breezy"].fetch("acme")[0].company == "Acme"


def test_personio_xml(monkeypatch):
    xml = (
        b"<workzag-jobs><position><id>9</id><name>Dev</name>"
        b"<office>Munich</office>\n    "
        b"<createdAt>2026-10-01T00:00:00+00:00</createdAt>"
        b"<jobDescriptions><jobDescription><name>About</name>"
        b"<value>&lt;p&gt;Hi&lt;/p&gt;</value></jobDescription>"
        b"</jobDescriptions></position></workzag-jobs>"
    )
    patch(monkeypatch, FakeResp(content=xml))
    [j] = VENDORS["personio"].fetch("acme.de")
    assert (
        j.url == "https://acme.jobs.personio.de/job/9"
        and "Hi" in j.description
    )


def test_teamtailor_rss(monkeypatch):
    xml = b"""<rss xmlns:tt="https://teamtailor.com/locations"><channel><item><title>Eng</title><link>https://a/jobs/1</link>
    <pubDate>Mon, 01 Oct 2026</pubDate><description>desc</description>
    <tt:locations><tt:location><tt:city>Oslo</tt:city><tt:country>Norway</tt:country></tt:location></tt:locations></item></channel></rss>"""
    patch(monkeypatch, FakeResp(content=xml))
    [j] = VENDORS["teamtailor"].fetch("acme")
    assert j.location == "Oslo, Norway"


def test_workday_paginates(monkeypatch):
    pages = [
        FakeResp(
            {
                "total": 21,
                "jobPostings": [
                    {
                        "title": str(i),
                        "externalPath": f"/job/{i}",
                        "postedOn": "Posted Today",
                    }
                    for i in range(20)
                ],
            }
        ),
        FakeResp(
            {
                "total": 21,
                "jobPostings": [{"title": "last", "externalPath": "/job/z"}],
            }
        ),
    ]
    from jobmcp import http

    monkeypatch.setattr(http, "post", lambda *a, **k: pages.pop(0))
    jobs = VENDORS["workday"].fetch("nvidia.wd5/Site")
    assert (
        len(jobs) == 21
        and jobs[0].url == "https://nvidia.wd5.myworkdayjobs.com/Site/job/0"
    )


# ── iCIMS ───────────────────────────────────────────────────────────────
ICIMS_SITEMAP = b"""<?xml version="1.0"?><urlset>
<url><loc>https://careers-acme.icims.com/jobs/search</loc></url>
<url><loc>https://careers-acme.icims.com/jobs/intro</loc></url>
<url><loc>https://careers-acme.icims.com/jobs/5417/senior-dentist/job</loc></url>
<url><loc>https://careers-acme.icims.com/jobs/5418/nurse-practitioner/job</loc></url>
</urlset>"""

ICIMS_PAGE = """<html><script type="application/ld+json">{"@context":"x",
"@type":"JobPosting","title":"Senior Dentist",
"datePosted":"2026-11-11T05:00:00.000Z",
"description":"<h2>Overview</h2><p>Care for people.</p><ul><li>DDS</li></ul>",
"hiringOrganization":{"@type":"Organization","name":"Acme Care"},
"jobLocation":[{"address":{"addressLocality":"Erie","addressRegion":"PA",
"addressCountry":"US","streetAddress":"UNAVAILABLE"}}]}</script></html>"""


def test_icims_board_keys_and_urls():
    v = VENDORS["icims"]
    assert (
        v.board_from_url("https://careers-acme.icims.com/jobs/1/x/job")
        == "careers-acme"
    )
    assert (
        v.board_from_url("https://1stheritage.icims.com/jobs/intro")
        == "1stheritage"
    )
    assert v.board_from_url("https://www.icims.com/") is None
    assert v.board_from_url("https://a.b.icims.com/x") is None
    assert v.normalize_board("Acme") == "careers-acme"
    assert v.normalize_board("careers-acme") == "careers-acme"
    assert (
        v.normalize_board("https://jobs-acme.icims.com/jobs/search")
        == "jobs-acme"
    )
    with pytest.raises(ValueError):
        v.normalize_board("www")


def test_icims_posting_json_ld():
    d = VENDORS["icims"].parse_posting(ICIMS_PAGE)
    assert d["title"] == "Senior Dentist" and d["company"] == "Acme Care"
    assert d["location"] == "Erie, PA, US"  # 'UNAVAILABLE' parts are dropped
    assert (
        "Care for people." in d["description"] and "- DDS" in d["description"]
    )
    assert VENDORS["icims"].parse_posting("<html>nothing</html>") == {}


def test_icims_fetch_reads_the_sitemap_then_each_new_posting(monkeypatch):
    from jobmcp import http

    pages = {"sitemap": FakeResp(content=ICIMS_SITEMAP)}
    seen = []

    def get(url, **kw):
        seen.append(url)
        if url.endswith("sitemap.xml"):
            return pages["sitemap"]
        if "5417" in url:
            r = FakeResp(content=ICIMS_PAGE.encode())
            return r
        raise RuntimeError("this posting page is down")

    monkeypatch.setattr(http, "get", get)
    jobs = VENDORS["icims"].fetch("careers-acme")
    by_id = {j.url.split("/jobs/")[1].split("/")[0]: j for j in jobs}
    assert set(by_id) == {"5417", "5418"}  # search / intro pages are skipped
    assert by_id["5417"].title == "Senior Dentist" and by_id["5417"].posted_at
    assert (
        by_id["5418"].title == "Nurse Practitioner"
    )  # failed page: title from the url
    assert by_id["5418"].location == "" and by_id["5418"].description == ""

    seen.clear()  # next sync: what is already stored is not fetched again
    prev = {j.url: j for j in jobs}
    VENDORS["icims"].fetch("careers-acme", previous=prev)
    assert [u for u in seen if "/jobs/" in u and "5417" in u] == []
    assert any(
        "5418" in u for u in seen
    )  # the one without a description is retried


def test_icims_not_a_sitemap_is_a_soft_block_not_an_empty_board(monkeypatch):
    from jobmcp import http
    from jobmcp.vendors.base import SoftBlocked

    monkeypatch.setattr(
        http,
        "get",
        lambda *a, **k: FakeResp(content=b"<html>Access denied</html>"),
    )
    with pytest.raises(SoftBlocked):
        VENDORS["icims"].fetch("careers-acme")


# ── Paylocity ───────────────────────────────────────────────────────────
GUID_ = "9597a587-1c23-4def-9ecc-4901e647fab7"
PAYLOCITY_PAGE = (
    "<html><script>var x=1;</script><script>window.pageData = "
    '{"CompanyName":"Innovation &amp; Work","Jobs":[{"JobId":4333171,'
    '"JobTitle":"Telephone Operator",'
    '"PublishedDate":"2026-09-23T06:57:44-05:00",'
    '"Description":"\\u00a0We need <b>you</b>; braces } in text {",'
    '"JobLocation":{"City":"Erie","State":"PA","Country":"USA"},"IsRemote":false},'
    '{"JobId":4333172,"JobTitle":"Analyst","JobLocation":null,"IsRemote":true,'
    '"LocationName":"Main"}]};</script></html>'
)


def test_paylocity_board_keys():
    v = VENDORS["paylocity"]
    url = f"https://recruiting.paylocity.com/recruiting/jobs/All/{GUID_.upper()}/Acme"
    assert v.board_from_url(url) == GUID_
    assert (
        v.board_from_url(
            "https://recruiting.paylocity.com/Recruiting/Jobs/Details/1"
        )
        is None
    )
    assert v.normalize_board(url) == GUID_
    with pytest.raises(ValueError):
        v.normalize_board("acme")


def test_paylocity_parse(monkeypatch):
    patch(monkeypatch, FakeResp(content=PAYLOCITY_PAGE.encode()))
    jobs = VENDORS["paylocity"].fetch(GUID_)
    assert [j.title for j in jobs] == ["Telephone Operator", "Analyst"]
    a, b = jobs
    assert a.company == "Innovation & Work" and a.location == "Erie, PA, USA"
    assert a.url.endswith("/Recruiting/Jobs/Details/4333171")
    assert "braces } in text {" in a.description and "you" in a.description
    assert b.location == "Remote"  # no address: remote flag, else the name


def test_paylocity_full_description_comes_from_the_posting_page(monkeypatch):
    from jobmcp import http

    ld = (
        '<script type="application/ld+json">{"@type":"JobPosting",'
        '"description":"<p>The whole text</p><ul><li>one</li></ul>"}</script>'
    )
    asked = []

    def get(url, **k):
        asked.append(url)
        if "/Jobs/Details/" in url:
            return FakeResp(content=ld.encode())
        return FakeResp(content=PAYLOCITY_PAGE.encode())

    monkeypatch.setattr(http, "get", get)
    jobs = VENDORS["paylocity"].fetch(GUID_)
    assert "The whole text" in jobs[0].description
    assert "- one" in jobs[0].description
    assert len([u for u in asked if "/Jobs/Details/" in u]) == 2

    # a second run holds the full text already: no posting page is fetched
    asked.clear()
    prev = {j.url: j for j in jobs}
    again = VENDORS["paylocity"].fetch(GUID_, previous=prev)
    assert asked and not [u for u in asked if "/Jobs/Details/" in u]
    assert again[0].description == jobs[0].description


def test_paylocity_names_the_company_not_the_guid(monkeypatch):
    from jobmcp import http

    page = (
        '<script>window.pageData = {"ModuleTitle":"116508 Innovation At '
        'Work","Jobs":[{"JobId":1,"JobTitle":"A","Description":"x"}]};'
        "</script>"
    )
    ld = (
        '<script type="application/ld+json">{"@type":"JobPosting",'
        '"description":"Full","hiringOrganization":{"name":"Innovation At '
        'Work Inc"}}</script>'
    )

    def get(url, **k):
        body = ld if "/Jobs/Details/" in url else page
        return FakeResp(content=body.encode())

    monkeypatch.setattr(http, "get", get)
    v = VENDORS["paylocity"]
    first = v.fetch(GUID_)
    assert first[0].company == "Innovation At Work Inc"
    # no page fetched this time: the stored name is kept, not re-guessed
    again = v.fetch(GUID_, previous={j.url: j for j in first})
    assert again[0].company == "Innovation At Work Inc"
    # nothing stored and no posting page: the page title, minus the number
    monkeypatch.setattr(
        http,
        "get",
        lambda url, **k: (
            FakeResp(content=b"", data=None)
            if "/Jobs/Details/" in url
            else FakeResp(content=page.encode())
        ),
    )
    assert v.fetch(GUID_)[0].company == "Innovation At Work"


def test_paylocity_keeps_the_snippet_when_the_posting_page_fails(monkeypatch):
    from jobmcp import http

    def get(url, **k):
        if "/Jobs/Details/" in url:
            raise OSError("down")
        return FakeResp(content=PAYLOCITY_PAGE.encode())

    monkeypatch.setattr(http, "get", get)
    jobs = VENDORS["paylocity"].fetch(GUID_)
    assert "braces } in text {" in jobs[0].description  # the board still syncs


def test_paylocity_page_without_a_job_list_is_a_soft_block(monkeypatch):
    from jobmcp.vendors.base import SoftBlocked

    patch(
        monkeypatch,
        FakeResp(content=b"<html>please verify you are human</html>"),
    )
    with pytest.raises(SoftBlocked):
        VENDORS["paylocity"].fetch(GUID_)


# ── Workday throttling ──────────────────────────────────────────────────
def test_workday_total_changing_midway_is_a_soft_block(monkeypatch):
    from jobmcp import http
    from jobmcp.vendors.base import SoftBlocked

    pages = iter(
        [
            {
                "total": 45,
                "jobPostings": [
                    {"title": f"J{i}", "externalPath": f"/job/{i}"}
                    for i in range(20)
                ],
            },
            {
                "total": 3,
                "jobPostings": [{"title": "J", "externalPath": "/job/x"}] * 3,
            },
        ]
    )
    monkeypatch.setattr(http, "post", lambda *a, **k: FakeResp(next(pages)))
    with pytest.raises(SoftBlocked, match="total changed"):
        VENDORS["workday"].fetch("acme.wd5/Careers")


def test_workday_empty_page_before_the_total_is_a_soft_block(monkeypatch):
    from jobmcp import http
    from jobmcp.vendors.base import SoftBlocked

    pages = iter(
        [
            {
                "total": 45,
                "jobPostings": [
                    {"title": f"J{i}", "externalPath": f"/job/{i}"}
                    for i in range(20)
                ],
            },
            {"total": 45, "jobPostings": []},
        ]
    )
    monkeypatch.setattr(http, "post", lambda *a, **k: FakeResp(next(pages)))
    with pytest.raises(SoftBlocked, match="empty page"):
        VENDORS["workday"].fetch("acme.wd5/Careers")


def test_workday_total_is_only_given_on_the_first_page(monkeypatch):
    """Real Workday answers `total` on page one and 0 afterwards. That is
    normal, not throttling."""
    from jobmcp import http

    def page(n, total):
        return {
            "total": total,
            "jobPostings": [
                {"title": f"J{n}-{i}", "externalPath": f"/job/{n}-{i}"}
                for i in range(20 if n < 2 else 5)
            ],
        }

    pages = iter([page(0, 45), page(1, 0), page(2, 0)])
    monkeypatch.setattr(http, "post", lambda *a, **k: FakeResp(next(pages)))
    jobs = VENDORS["workday"].fetch("acme.wd5/Careers")
    assert len(jobs) == 45


def test_gem_parse(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            [
                {
                    "title": " Senior Android Engineer ",
                    "absolute_url": "https://jobs.gem.com/fetch/7536752003",
                    "content": "<h3>Role</h3><div>Build apps.</div>"
                    "<ul><li>Kotlin</li><li>Compose</li></ul>",
                    "content_plain": "Role Build apps. Kotlin Compose",
                    "first_published_at": "2026-05-12T22:51:02.331Z",
                    "created_at": "2025-11-21T22:26:01.000Z",
                    "location": {"name": "New York, United States"},
                    "location_type": "hybrid",
                },
                {
                    "title": "Designer",
                    "absolute_url": "https://jobs.gem.com/fetch/2",
                    "content": "",
                    "content_plain": "Plain only",
                    "created_at": "2026-01-02T00:00:00.000Z",
                    "location": {"name": "United States - Remote"},
                    "location_type": "remote",
                },
                {
                    "title": "Onsite",
                    "absolute_url": "https://jobs.gem.com/fetch/3",
                    "content": "<p>x</p>",
                    "location": None,
                    "location_type": "in_office",
                },
            ]
        ),
    )
    a, b, c = VENDORS["gem"].fetch("fetch")
    assert a.title == "Senior Android Engineer" and a.company == "fetch"
    assert a.url == "https://jobs.gem.com/fetch/7536752003"
    assert a.posted_at.startswith("2026-05-12")
    assert a.description == "Role\n\nBuild apps.\n\n- Kotlin\n- Compose"
    assert a.location == "New York, United States (Hybrid)"  # for the label
    assert b.description == "Plain only"  # falls back to the plain text
    assert b.posted_at.startswith("2026-01-02")  # never published: created
    assert b.location == "United States - Remote"  # not said twice
    assert c.location == ""  # in-office adds nothing


def test_gem_slugs_are_lowercase_and_a_missing_board_is_a_404():
    assert VENDORS["gem"].normalize_board(" Function-Health/ ") == (
        "function-health"
    )
    assert VENDORS["gem"].public_url("fetch") == "https://jobs.gem.com/fetch"
