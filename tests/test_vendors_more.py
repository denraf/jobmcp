"""The vendors added after the first fourteen: each is parsed from a small
fixture shaped like the live response."""

import json

import pytest

from jobmcp import http
from jobmcp.vendors import VENDORS, classify

from .test_vendors import FakeResp


def routes(monkeypatch, table):
    """http.get answers with the first fixture whose key is in the URL."""

    def get(url, **kw):
        for key, resp in table:
            if key in url:
                resp.url = url  # a real response knows where it ended up
                return resp
        return FakeResp(content=b"", data=None) if False else _missing()

    def _missing():
        r = FakeResp(content=b"not found")
        r.status_code = 404
        return r

    monkeypatch.setattr(http, "get", get)


def html(s: str) -> FakeResp:
    return FakeResp(content=s.encode())


def ld(**kw) -> str:
    data = {"@context": "https://schema.org", "@type": "JobPosting", **kw}
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


POSTING = ld(
    title="Data Engineer",
    description="<p>Build <b>pipes</b></p><ul><li>SQL</li></ul>",
    datePosted="2026-10-01",
    hiringOrganization={"@type": "Organization", "name": "Acme Corp"},
    jobLocation={
        "@type": "Place",
        "address": {"addressLocality": "Berlin", "addressCountry": "DE"},
    },
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://acme.pinpointhq.com/en/postings/1", ("pinpoint", "acme")),
        ("https://www.pinpointhq.com/pricing", None),
        ("https://ats.rippling.com/astra/jobs/abc", ("rippling", "astra")),
        (
            "https://ats.rippling.com/api/v2/board/Astra/jobs",
            ("rippling", "astra"),
        ),
        ("https://jobs.jobvite.com/cetera/job/oCm", ("jobvite", "cetera")),
        ("https://jobs.jobvite.com/job/oCm", None),
        ("https://jki.applytojob.com/apply/x/Eng", ("applytojob", "jki")),
        ("https://acme.homerun.co/engineer", ("homerun", "acme")),
        ("https://acme.careerplug.com/jobs/1", ("careerplug", "acme")),
        ("https://acme.softgarden.io/vacancies", ("softgarden", "acme")),
        ("https://acme.agilehr.com/x", ("agilehr", "acme")),
        ("https://acme.freshteam.com/jobs", ("freshteam", "acme")),
        ("https://assets.freshteam.com/x.js", None),
        (
            "https://recruiterflow.com/jenix/jobs/71",
            ("recruiterflow", "jenix"),
        ),
        (
            "https://www.comeet.com/jobs/biocatch/03.00E/ai-eng/AB.12",
            ("comeet", "biocatch/03.00E"),
        ),
        ("https://join.com/companies/novago/123-x", ("join", "novago")),
        ("https://join.com/jobs", None),
        ("https://acme.careers.hibob.com/jobs/1", ("hibob", "acme")),
    ],
)
def test_classify(url, expected):
    assert classify(url) == expected


def test_pinpoint(monkeypatch):
    routes(
        monkeypatch,
        [
            (
                "postings.json",
                FakeResp(
                    {
                        "data": [
                            {
                                "title": "Manager",
                                "url": "https://acme.pinpointhq.com/en/postings/1",
                                "description": "<p>About us</p>",
                                "key_responsibilities": (
                                    "<ul><li>Lead</li></ul>"
                                ),
                                "key_responsibilities_header": "ROLE",
                                "workplace_type": "remote",
                                "location": {
                                    "city": "Belfast",
                                    "name": "Belfast",
                                },
                            }
                        ]
                    }
                ),
            )
        ],
    )
    [j] = VENDORS["pinpoint"].fetch("acme")
    assert j.title == "Manager" and j.location == "Remote Belfast"
    assert j.description.startswith("About us") and "ROLE\n" in j.description


def test_rippling_lists_then_reads_each_posting(monkeypatch):
    calls = []

    def get(url, **kw):
        calls.append(url)
        if url.endswith("/jobs/j1"):
            return FakeResp(
                {
                    "description": {
                        "company": "<p>Co</p>",
                        "role": "<p>Role</p>",
                    },
                    "companyName": " Astra",
                    "createdOn": "2026-07-11T17:22:12-07:00",
                }
            )
        return FakeResp(
            {
                "items": [
                    {
                        "id": "j1",
                        "name": "Engineer",
                        "url": "https://ats.rippling.com/astra/jobs/j1",
                        "locations": [{"name": "Alameda, CA"}],
                    }
                ],
                "totalPages": 1,
            }
        )

    monkeypatch.setattr(http, "get", get)
    [j] = VENDORS["rippling"].fetch("astra")
    assert (j.company, j.location, j.posted_at[:10]) == (
        "Astra",
        "Alameda, CA",
        "2026-07-11",
    )
    assert j.description == "Co\n\nRole"
    # a posting whose text is already stored is not asked for again
    calls.clear()
    VENDORS["rippling"].fetch("astra", previous={j.url: j})
    assert not any(c.endswith("/jobs/j1") for c in calls)


def test_jobvite(monkeypatch):
    lst = (
        '<h3>Sales</h3><table class="jv-job-list"><tr>'
        '<td class="jv-job-list-name"><a href="/cetera/job/oCm">Sales '
        "Associate</a></td>"
        '<td class="jv-job-list-location">Cleveland, Ohio</td></tr></table>'
    )
    routes(
        monkeypatch,
        [("/job/oCm", html(POSTING)), ("/cetera/jobs", html(lst))],
    )
    [j] = VENDORS["jobvite"].fetch("cetera")
    assert j.url == "https://jobs.jobvite.com/cetera/job/oCm"
    assert (j.title, j.company) == ("Data Engineer", "Acme Corp")
    assert j.description == "Build pipes\n\n- SQL" or "pipes" in j.description


def test_applytojob_reads_the_description_block(monkeypatch):
    lst = (
        '<ul><li class="list-group-item"><h3 class="list-group-item-heading">'
        '<a href="https://jki.applytojob.com/apply/x1/Engineer">Engineer</a>'
        '</h3><ul><li><i class="fa fa-map-marker"></i> Walnut Creek, CA</li>'
        "</ul></li></ul>"
    )
    page = (
        '<div class="description" id="job-description"><p>Great job</p></div>'
        '<div class="resumator-mobile-apply-wrapper"></div>'
    )
    routes(
        monkeypatch,
        [("/apply/x1", html(page)), ("/apply", html(lst))],
    )
    [j] = VENDORS["applytojob"].fetch("jki")
    assert (j.title, j.location, j.description) == (
        "Engineer",
        "Walnut Creek, CA",
        "Great job",
    )


def test_homerun(monkeypatch):
    payload = {
        "content": {
            "vacancies": [
                {
                    "url": "https:\\/\\/acme.homerun.co\\/eng",
                    "title": "Eng",
                    "location_id": 7,
                }
            ],
            "locations": [{"id": 7, "name": "Leiden"}],
        }
    }
    page = "<job-list v-bind='%s'></job-list>" % json.dumps(payload).replace(
        '"', "&quot;"
    ).replace("\\\\/", "\\/")
    routes(monkeypatch, [("/eng", html(POSTING)), ("homerun.co", html(page))])
    [j] = VENDORS["homerun"].fetch("acme")
    assert j.url == "https://acme.homerun.co/eng"
    assert j.location == "Berlin, DE" and "pipes" in j.description


def test_careerplug(monkeypatch):
    lst = (
        '<a aria-label="Open Cook" href="/jobs/55">'
        '<div class="job-title">Cook</div>'
        '<div class="job-location">Location: Austin, TX</div></a>'
    )
    routes(monkeypatch, [("/jobs/55", html(POSTING)), ("/jobs", html(lst))])
    [j] = VENDORS["careerplug"].fetch("acme")
    assert j.url == "https://acme.careerplug.com/jobs/55"
    assert "pipes" in j.description


def test_softgarden(monkeypatch):
    lst = (
        '<div class="matchElement" id="job_id_9"><a href="/job/9/Eng">Eng</a>'
        '<span class="location-view-item">Berlin,</span>'
        '<span class="location-view-item">Germany</span></div>'
    )
    routes(monkeypatch, [("/job/9", html(POSTING)), ("/vacancies", html(lst))])
    [j] = VENDORS["softgarden"].fetch("acme")
    assert j.url == "https://acme.softgarden.io/job/9/Eng"


def test_agilehr(monkeypatch):
    routes(
        monkeypatch,
        [
            (
                "careerportal",
                FakeResp(
                    {
                        "ResultList": [
                            {
                                "Title": "Architect",
                                "ApplyUrl": "https://x.agilehr.com/a",
                                "Location": "Dallas",
                                "StartDate": "01/01/1900 00:00:00",
                                "OpenDate": "2026-10-01T00:00:00",
                                "LongDescription": "<p>Design</p>",
                            }
                        ]
                    }
                ),
            )
        ],
    )
    [j] = VENDORS["agilehr"].fetch("x")
    assert j.posted_at == "2026-10-01T00:00:00" and j.description == "Design"


def test_freshteam(monkeypatch):
    lst = (
        '<a href="/jobs/AbC/eng" class="heading" '
        'data-portal-location="Hollywood">'
        '<div class="job-title">Eng</div>'
        '<div class="location-info">Remote</div></a>'
    )
    routes(monkeypatch, [("/jobs/AbC", html(POSTING)), ("/jobs", html(lst))])
    [j] = VENDORS["freshteam"].fetch("acme")
    assert j.url == "https://acme.freshteam.com/jobs/AbC/eng"
    assert j.company == "Acme Corp"


def test_recruiterflow(monkeypatch):
    jobs = {
        "department": [
            [
                "Data",
                [
                    {
                        "apply_link": "jenix/jobs/71",
                        "job_name": "Architect",
                        "details": "Amsterdam",
                        "last_opened": "2025-10-04T18:14:16+0000",
                    }
                ],
            ]
        ]
    }
    page = f"<script>window.jobsList = {json.dumps(jobs)};</script>"
    routes(
        monkeypatch, [("/jobs/71", html(POSTING)), ("/jenix/jobs", html(page))]
    )
    [j] = VENDORS["recruiterflow"].fetch("jenix")
    assert j.url == "https://recruiterflow.com/jenix/jobs/71"
    assert j.location == "Berlin, DE"  # the posting page is the authority


def test_comeet(monkeypatch):
    items = [
        {
            "name": "Engineer",
            "company_name": "BioCatch",
            "url_comeet_hosted_page": "https://www.comeet.com/jobs/b/1/eng/A",
            "location": {"name": "Tel Aviv"},
            "workplace_type": "Remote",
            "time_updated": "2026-11-13T12:54:13Z",
            "custom_fields": {
                "details": [
                    {"name": "Description", "value": "<p>Build</p>"},
                    {"name": "Requirements", "value": "<p>Python</p>"},
                ]
            },
        }
    ]
    page = f"<script>COMPANY_POSITIONS_DATA = {json.dumps(items)};</script>"
    routes(monkeypatch, [("comeet.com/jobs/b/1", html(page))])
    [j] = VENDORS["comeet"].fetch("b/1")
    assert j.company == "BioCatch" and j.location == "Remote Tel Aviv"
    assert j.description == "Build\n\nRequirements\nPython"


def test_comeet_board_key_is_two_parts():
    v = VENDORS["comeet"]
    assert v.normalize_board(
        "https://www.comeet.com/jobs/biocatch/03.00E"
    ) == ("biocatch/03.00E")
    with pytest.raises(ValueError):
        v.normalize_board("biocatch")


def test_join(monkeypatch):
    data = {
        "props": {
            "pageProps": {
                "pageData": {
                    "company": {"name": "NovaGo"},
                    "jobs": {
                        "items": [
                            {
                                "idParam": "1-eng",
                                "title": "Engineer",
                                "createdAt": "2026-01-01",
                                "city": {
                                    "cityName": "Wien",
                                    "countryName": "AT",
                                },
                            }
                        ],
                        "pagination": {"page": 1, "pageCount": 1},
                    },
                }
            }
        }
    }
    page = (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data)}</script>"
    )
    routes(
        monkeypatch,
        [("/novago/1-eng", html(POSTING)), ("/companies/novago", html(page))],
    )
    [j] = VENDORS["join"].fetch("novago")
    assert j.url == "https://join.com/companies/novago/1-eng"
    assert "pipes" in j.description


def test_hibob(monkeypatch):
    routes(
        monkeypatch,
        [
            (
                "api/job-ad",
                FakeResp(
                    {
                        "jobAdDetails": [
                            {
                                "id": "u1",
                                "title": "AE",
                                "site": "London",
                                "country": "UK",
                                "workspaceType": "Remote",
                                "publishedAt": "2026-10-27T15:21:38Z",
                                "description": "About",
                                "requirements": "<ul><li>Sales</li></ul>",
                                "sectionLabels": {"requirements": "You have"},
                            }
                        ]
                    }
                ),
            )
        ],
    )
    [j] = VENDORS["hibob"].fetch("acme")
    assert j.url == "https://acme.careers.hibob.com/jobs/u1"
    assert j.location == "Remote London, UK"
    assert j.description == "About\n\nYou have\n- Sales"


# -- second group -------------------------------------------------------

GUID = "40c1c711-7dcf-4fbb-9254-e2e3d7d2b1aa"


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://app.careerpuck.com/job-board/Lyft/job/1",
            ("careerpuck", "lyft"),
        ),
        (
            "https://web.fountain.com/c/clear/clear/US/airport-agent",
            ("fountain", "clear/clear/US"),
        ),
        ("https://web.fountain.com/c/clear", None),
        ("https://rampersand.getro.com/jobs", ("getro", "rampersand")),
        ("https://jobs.crelate.com/portal/acme/job/x", ("crelate", "acme")),
        ("https://www.careers-page.com/stone/job/AB", ("manatal", "stone")),
        ("https://stone.careers-page.com/", ("manatal", "stone")),
        ("https://www.careers-page.com/job/AB", None),
        ("https://acme.zohorecruit.com/jobs/Careers/1", ("zoho", "acme")),
        ("https://esncc.applicantpro.com/jobs/12", ("applicantpro", "esncc")),
        (
            f"https://recruiting.ultipro.com/BRU1001BRUCK/JobBoard/{GUID}/?q=",
            ("ultipro", f"BRU1001BRUCK/{GUID}"),
        ),
        (
            f"https://hopnc.rec.pro.ukg.net/HOS1023/JobBoard/{GUID.upper()}",
            ("ukg", f"hopnc/HOS1023/{GUID}"),
        ),
        (
            "https://www.ycombinator.com/companies/ambient-ai/jobs/x-y",
            ("ycombinator", "ambient-ai"),
        ),
        ("https://www.ycombinator.com/companies/ambient-ai", None),
        ("https://neodyne.occupop-careers.com/job/1", ("occupop", "neodyne")),
        (
            "https://contextmenu-neodyne.occupop-careers.com/",
            ("occupop", "neodyne"),
        ),
        ("https://gp-krk.talentlyft.com/jobs/x", ("talentlyft", "gp-krk")),
    ],
)
def test_classify_second_group(url, expected):
    assert classify(url) == expected


def post_json(monkeypatch, fn):
    monkeypatch.setattr(http, "post", lambda url, **kw: FakeResp(fn(url, kw)))


def test_careerpuck(monkeypatch):
    routes(
        monkeypatch,
        [
            (
                "job-boards/lyft",
                FakeResp(
                    {
                        "employerProfile": {"name": "Lyft"},
                        "jobs": [
                            {
                                "title": " Counsel ",
                                "status": "public",
                                "location": "Houston, TX",
                                "workplaceType": "remote",
                                "publicUrl": "https://app.careerpuck.com/x/1",
                                "postedAt": "2026-11-13T20:16:04Z",
                                "content": (
                                    "&lt;p&gt;At Lyft&comma; we "
                                    "&lt;b&gt;serve&lt;/b&gt;&lt;/p&gt;"
                                ),
                            },
                            {"title": "Draft", "status": "draft"},
                        ],
                    }
                ),
            )
        ],
    )
    [j] = VENDORS["careerpuck"].fetch("lyft")
    assert (j.company, j.title, j.location) == (
        "Lyft",
        "Counsel",
        "Remote Houston, TX",
    )
    assert j.description == "At Lyft, we serve"


def test_fountain_lists_every_page(monkeypatch):
    pages = {
        "1": {
            "openings": [
                {"title": "A", "to_param": "a", "location_name": "Austin"}
            ],
            "pagination": {"total_pages": 2},
        },
        "2": {
            "openings": [{"title": "B", "to_param": "b"}],
            "pagination": {"total_pages": 2},
        },
    }
    monkeypatch.setattr(
        http,
        "get",
        lambda url, **kw: FakeResp(pages[str(kw["params"]["page"])]),
    )
    jobs = VENDORS["fountain"].fetch("clear/clear/US")
    assert [j.title for j in jobs] == ["A", "B"]
    assert jobs[0].url == "https://web.fountain.com/c/clear/clear/US/a"


def next_data(**page_data) -> str:
    data = {"props": {"pageProps": page_data}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data)}</script>"
    )


def test_getro(monkeypatch):
    page = next_data(
        initialState={
            "jobs": {
                "found": [
                    {
                        "title": "QA",
                        "slug": "1-qa",
                        "organization": {
                            "slug": "booknook",
                            "name": "BookNook",
                        },
                        "searchableLocations": ["Remote United States"],
                        "createdAt": 1760000000,
                    }
                ]
            }
        }
    )
    routes(
        monkeypatch,
        [
            ("/companies/booknook/jobs/1-qa", html(POSTING)),
            ("/jobs", html(page)),
        ],
    )
    [j] = VENDORS["getro"].fetch("lumos")
    assert j.url == "https://lumos.getro.com/companies/booknook/jobs/1-qa"
    assert j.location == "Berlin, DE" and j.company == "Acme Corp"


def test_crelate(monkeypatch):
    seen = {}

    def get(url, **kw):
        if "candidateportal" in url:
            seen["envelope"] = json.loads(kw["params"]["requestEnvelope"])
            return FakeResp(
                {
                    "Jobs": [
                        {
                            "Title": "SLP",
                            "Url": "/abc123",
                            "City": "Fort Myers",
                            "State": "FL",
                            "LastPostedOnDate": "2026-11-13",
                            "Description": "<p>Therapy</p>",
                        }
                    ]
                }
            )
        return html("<script>var ORG_ID = 'aaaaaaaa-1111-2222';</script>")

    monkeypatch.setattr(http, "get", get)
    [j] = VENDORS["crelate"].fetch("careertree")
    assert seen["envelope"]["OrganizationId"] == "aaaaaaaa-1111-2222"
    assert j.url == "https://jobs.crelate.com/portal/careertree/job/abc123"
    assert (j.location, j.description) == ("Fort Myers, FL", "Therapy")


def test_manatal_follows_the_next_page(monkeypatch):
    pages = {
        "first": {
            "results": [
                {"hash": "H1", "position_name": "A", "description": "<p>x</p>"}
            ],
            "next": "https://www.careers-page.com/api/v1.0/c/s/jobs/?page=2",
        },
        "page=2": {
            "results": [
                {
                    "hash": "H2",
                    "position_name": "B",
                    "location_display": "Renton, WA",
                    "is_remote": True,
                }
            ],
            "next": None,
        },
    }
    monkeypatch.setattr(
        http,
        "get",
        lambda url, **kw: FakeResp(
            pages["page=2" if "page=2" in url else "first"]
        ),
    )
    jobs = VENDORS["manatal"].fetch("s")
    assert [(j.title, j.url) for j in jobs] == [
        ("A", "https://www.careers-page.com/s/job/H1"),
        ("B", "https://www.careers-page.com/s/job/H2"),
    ]
    assert jobs[1].location == "Remote Renton, WA"


def test_zoho(monkeypatch):
    items = [
        {
            "id": "7",
            "Posting_Title": "Abogado",
            "City": "Medellin",
            "Country": "Colombia",
            "Publish": "True",
            "Date_Opened": "2026-10-26",
            "Job_Description": "<p>Queremos</p>",
        },
        {"id": "8", "Posting_Title": "Hidden", "Publish": False},
    ]
    from html import escape

    page = (
        f'<input type="hidden" value="{escape(json.dumps(items))}" id="jobs">'
    )
    routes(monkeypatch, [("/jobs/Careers", html(page))])
    [j] = VENDORS["zoho"].fetch("acme")
    assert j.url == "https://acme.zohorecruit.com/jobs/Careers/7"
    assert (j.location, j.description) == ("Medellin, Colombia", "Queremos")


def test_applicantpro(monkeypatch):
    def get(url, **kw):
        if "/core/jobs/6852" in url:
            return FakeResp(
                {
                    "success": True,
                    "data": {
                        "jobs": [
                            {
                                "id": 5,
                                "title": "Engineer",
                                "jobUrl": "https://esncc.applicantpro.com/jobs/5",
                                "jobLocation": "Chesapeake, VA",
                                "workplaceType": "Remote",
                                "startDateRef": "Oct 26, 2026",
                            }
                        ]
                    },
                }
            )
        if url.endswith("/jobs/5"):
            return html(POSTING)
        return html("<script>var domain_id = '6852';</script>")

    monkeypatch.setattr(http, "get", get)
    [j] = VENDORS["applicantpro"].fetch("esncc")
    assert j.location == "Berlin, DE" and "pipes" in j.description


def test_ultipro_lists_then_reads_the_page(monkeypatch):
    sent = []

    def post(url, **kw):
        sent.append((url, kw["json"]["opportunitySearch"]["Skip"]))
        return FakeResp(
            {
                "totalCount": 1,
                "opportunities": [
                    {
                        "Id": "op1",
                        "Title": "Driver",
                        "PostedDate": "2026-02-18T17:50:04Z",
                        "BriefDescription": "<p>Short</p>",
                        "Locations": [
                            {
                                "Address": {
                                    "City": "Wenatchee",
                                    "State": {"Code": "WA"},
                                    "Country": {"Name": "United States"},
                                }
                            }
                        ],
                    }
                ],
            }
        )

    detail = (
        'x CandidateOpportunityDetail({"Description": "<p>Full text</p>",'
        '"EqualOpportunityEmployerDescription": "EOE"});'
    )
    monkeypatch.setattr(http, "post", post)
    routes(monkeypatch, [("OpportunityDetail", html(detail))])
    board = f"BRU1001BRUCK/{GUID}"
    [j] = VENDORS["ultipro"].fetch(board)
    assert sent == [
        (
            f"https://recruiting.ultipro.com/BRU1001BRUCK/JobBoard/{GUID}"
            "/JobBoardView/LoadSearchResults",
            0,
        )
    ]
    assert j.location == "Wenatchee, WA, United States"
    assert j.description == "Full text\n\nEOE"

    # when the page cannot be read the teaser is better than nothing
    routes(monkeypatch, [])
    [j] = VENDORS["ultipro"].fetch(board)
    assert j.description == "Short"


def test_ukg_board_key_and_url():
    v = VENDORS["ukg"]
    key = v.normalize_board(
        f"https://TeamJCK.rec.pro.ukg.net/JCK1000JCKR/JobBoard/{GUID}/?q="
    )
    assert key == f"teamjck/JCK1000JCKR/{GUID}"
    assert v.base(key) == (
        f"https://teamjck.rec.pro.ukg.net/JCK1000JCKR/JobBoard/{GUID}"
    )
    with pytest.raises(ValueError):
        v.normalize_board("teamjck")


def test_ycombinator(monkeypatch):
    from html import escape

    props = {
        "company": {"name": "Ambient.ai"},
        "jobPostings": [
            {
                "title": "Engineer",
                "url": "/companies/ambient-ai/jobs/x1-engineer",
                "location": "SF Bay Area",
            }
        ],
    }
    page = (
        '<div id="WaasShowJobsPage-react-component-1" data-page="'
        + escape(json.dumps({"props": props}))
        + '"></div>'
    )
    routes(
        monkeypatch,
        [("jobs/x1-engineer", html(POSTING)), ("/jobs", html(page))],
    )
    seen = []
    real = http.get

    def spy(url, **kw):
        seen.append(kw.get("gate"))
        return real(url, **kw)

    monkeypatch.setattr(http, "get", spy)
    [j] = VENDORS["ycombinator"].fetch("ambient-ai")
    # the list page and the posting page both go through the host's one gate
    from jobmcp.vendors.ycombinator import GATE

    assert len(seen) == 2 and all(g is GATE for g in seen)
    assert (
        j.url
        == "https://www.ycombinator.com/companies/ambient-ai/jobs/x1-engineer"
    )
    assert j.company == "Acme Corp"  # the posting page names the organisation


def test_occupop(monkeypatch):
    post_json(
        monkeypatch,
        lambda url, kw: {
            "data": {
                "careersPage": {
                    "liveJobs": [
                        {
                            "uuid": "u1",
                            "title": "Driver",
                            "companyName": "Bow Distribution",
                            "publishedAt": "2026-06-25 11:43:41",
                            "description": "<p>Solid <b>pay</b></p>",
                            "location": {"city": "Alfreton", "country": "UK"},
                        }
                    ]
                }
            }
        },
    )
    [j] = VENDORS["occupop"].fetch("bow")
    assert j.url == "https://bow.occupop-careers.com/job/u1"
    assert (j.company, j.location, j.description) == (
        "Bow Distribution",
        "Alfreton, UK",
        "Solid pay",
    )


def test_talentlyft(monkeypatch):
    fragment = (
        '<a href="https://gp.talentlyft.com/jobs/arm-AcS" id="1" '
        'class="jobs__box">'
        '<h3 class="jobs__box__heading">Armira&#x10D;</h3>'
        '<p class="jobs__box__text">Krk, Croatia</p></a>'
        '<button data-page="1"></button>'
    )
    routes(
        monkeypatch,
        [("/jobs/arm-AcS", html(POSTING)), ("/JobList/", html(fragment))],
    )
    [j] = VENDORS["talentlyft"].fetch("gp")
    assert j.url == "https://gp.talentlyft.com/jobs/arm-AcS"
    assert "pipes" in j.description


# -- third group --------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://app.dover.com/jobs/laylo", ("dover", "laylo")),
        ("https://app.dover.com/apply/laylo/abc", ("dover", "laylo")),
        (
            "https://navistar.jibeapply.com/jobs/60706",
            ("jibeapply", "navistar"),
        ),
        (
            "https://recruitcrm.io/jobs/Talentbank_1_jobs",
            ("recruitcrm", "Talentbank_1_jobs"),
        ),
        ("https://zaxbys.isolvedhire.com/jobs/1", ("isolved", "zaxbys")),
        (
            "https://que-chido.prismhr-hire.com/job/9/x",
            ("prismhr", "que-chido"),
        ),
        ("https://login.prismhr-hire.com/", None),
    ],
)
def test_classify_third_group(url, expected):
    assert classify(url) == expected


def test_dover_has_no_text_but_names_the_company(monkeypatch):
    routes(
        monkeypatch,
        [
            (
                "careers-page-slug/laylo",
                FakeResp({"id": "c1", "name": "Laylo"}),
            ),
            (
                "job-groups/c1",
                FakeResp(
                    [
                        {
                            "name": "Ungrouped",
                            "jobs": [
                                {
                                    "id": "j1",
                                    "title": "Full Stack Engineer",
                                    "workplace_type": "REMOTE",
                                    "locations": [{"name": "United States"}],
                                },
                                {
                                    "id": "j2",
                                    "title": "Hidden",
                                    "is_published": False,
                                },
                            ],
                        }
                    ]
                ),
            ),
        ],
    )
    [j] = VENDORS["dover"].fetch("laylo")
    assert (j.company, j.location) == ("Laylo", "Remote United States")
    assert j.url == "https://app.dover.com/apply/laylo/j1"


def test_jibeapply_pages_until_the_total(monkeypatch):
    def job(n):
        return {
            "data": {
                "slug": str(n),
                "title": f"Job {n}",
                "description": "<p>Intro</p>",
                "responsibilities": "<p>Do</p>",
                "full_location": "Lisle, Illinois",
                "hiring_organization": "International",
                "posted_date": "2026-11-13T21:45:00+0000",
            }
        }

    pages = {1: [job(1), job(2)], 2: [job(3)]}
    monkeypatch.setattr(
        http,
        "get",
        lambda url, **kw: FakeResp(
            {"jobs": pages.get(kw["params"]["page"], []), "totalCount": 3}
        ),
    )
    jobs = VENDORS["jibeapply"].fetch("navistar")
    assert [j.url for j in jobs] == [
        f"https://navistar.jibeapply.com/jobs/{n}" for n in (1, 2, 3)
    ]
    assert jobs[0].description == "Intro\n\nDo"
    assert jobs[0].company == "International"


def test_recruitcrm_posts_the_account(monkeypatch):
    sent = {}

    def post(url, **kw):
        sent.update(kw)
        return FakeResp(
            {
                "data": {
                    "jobs": [
                        {
                            "slug": "s1",
                            "name": "Outreach",
                            "remote": "1",
                            "jdtext": "<p>Position: <b>Remote</b></p>",
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr(http, "post", post)
    [j] = VENDORS["recruitcrm"].fetch("VirtDrop")
    assert sent["params"]["account"] == "VirtDrop"
    assert (j.url, j.location) == (
        "https://recruitcrm.io/jobs/VirtDrop/s1",
        "Remote",
    )
    assert j.description == "Position: Remote"
    assert (
        VENDORS["recruitcrm"].normalize_board("recruitcrm.io/jobs/AbC")
        == "AbC"
    )


def test_isolved_uses_its_own_host(monkeypatch):
    seen = []

    def get(url, **kw):
        seen.append(url)
        if "/core/jobs/99" in url:
            return FakeResp({"data": {"jobs": [{"id": 1, "title": "Cook"}]}})
        if url.endswith("/jobs/1"):
            return html(POSTING)
        return html("domain_id = '99'")

    monkeypatch.setattr(http, "get", get)
    [j] = VENDORS["isolved"].fetch("zaxbys")
    assert j.url == "https://zaxbys.isolvedhire.com/jobs/1"
    assert all("isolvedhire.com" in u for u in seen)


def test_prismhr(monkeypatch):
    lst = (
        '<div class="job-headline"><a class="mobile-apply-link" '
        'href="/job/940644/line-cook"><h2>LINE COOK</h2></a>'
        '<div class="job-location"> Las Vegas, NV </div></div>'
        '<a href="/job/940644/line-cook"><h2>LINE COOK</h2></a>'
    )
    routes(
        monkeypatch, [("/job/940644", html(POSTING)), ("prismhr", html(lst))]
    )
    [j] = VENDORS["prismhr"].fetch("que-chido")
    assert j.url == "https://que-chido.prismhr-hire.com/job/940644/line-cook"
    assert "pipes" in j.description


# -- shared JSON-LD handling --------------------------------------------


def test_json_ld_survives_raw_newlines_and_prints_of_none():
    from jobmcp.vendors.detail import json_ld_posting, ld_fields

    page = (
        '<script type="application/ld+json">{"@type": "JobPosting", '
        '"title": "Lead", "description": "<p>line one\nline two</p>", '
        '"jobLocation": {"address": {"addressLocality": "Abidjan", '
        '"addressRegion": "None", "addressCountry": "null"}}}</script>'
    )
    f = ld_fields(json_ld_posting(page))
    assert f["location"] == "Abidjan"
    assert "line one" in f["description"] and "line two" in f["description"]


def test_a_page_with_another_json_ld_block_first_still_finds_the_posting():
    from jobmcp.vendors.detail import json_ld_posting

    page = (
        '<script type="application/ld+json">{"@type": "Organization"}</script>'
        + POSTING
    )
    assert json_ld_posting(page)["title"] == "Data Engineer"


def test_enrich_keeps_what_it_knew_when_a_page_is_not_fetched():
    from jobmcp.models import Job
    from jobmcp.vendors.detail import enrich

    def job(**kw):
        return Job(
            vendor="x",
            board="b",
            title="T",
            company="b",
            location="",
            url="u",
            **kw,
        )

    old = job(description="old text", posted_at="2026-01-01")
    old.company, old.location = "Acme", "Berlin"
    [j] = enrich([job()], {"u": old}, lambda u: pytest.fail("fetched"))
    assert (j.description, j.company, j.location, j.posted_at) == (
        "old text",
        "Acme",
        "Berlin",
        "2026-01-01",
    )


def test_trakstar(monkeypatch):
    lst = (
        '<div class="js-card js-careers-page-job-list-item" '
        'data-href="/jobs/fk1/"><a href="/jobs/fk1/">'
        '<h3 class="js-job-list-opening-name">Machinist</h3>'
        '<span class="meta-job-location-city">Lenexa</span>'
        '<span class="meta-job-location-state">Kansas</span></a></div>'
        '<div class="js-careers-page-job-list-item"><a href="/jobs/fk2/">'
        '<h3 class="js-job-list-opening-name">Welder</h3></a></div>'
    )
    routes(monkeypatch, [("/jobs/fk", html(POSTING)), ("/jobs", html(lst))])
    jobs = VENDORS["trakstar"].fetch("procharger")
    assert [j.url for j in jobs] == [
        "https://procharger.hire.trakstar.com/jobs/fk1/",
        "https://procharger.hire.trakstar.com/jobs/fk2/",
    ]
    assert classify("https://x.recruiterbox.com/jobs/abc/") == (
        "trakstar",
        "x",
    )


# -- fourth group -------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://ccyj.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/"
            "sites/CX/jobs",
            ("oracle", "ccyj.fa.us6/CX"),
        ),
        ("https://ccyj.fa.us6.oraclecloud.com/other", None),
        ("https://bms.eightfold.ai/careers/job/1", ("eightfold", "bms")),
        ("https://app.loxo.co/Brave-New-World", ("loxo", "Brave-New-World")),
        ("https://app.loxo.co/job/abc", None),
        (
            "https://hoffarch.simplicant.com/jobs/1-x/detail",
            ("simplicant", "hoffarch"),
        ),
        ("https://careers.pageuppeople.com/994", ("pageup", "994/cw")),
        (
            "https://careers.pageuppeople.com/994/cw/en-us/listing/",
            ("pageup", "994/cw"),
        ),
        (
            "https://jobs-shepherd.peopleadmin.com/postings/search",
            ("peopleadmin", "jobs-shepherd"),
        ),
    ],
)
def test_classify_fourth_group(url, expected):
    assert classify(url) == expected


def test_div_inner_keeps_nested_divs():
    from jobmcp.vendors.detail import div_inner

    page = (
        "<div id='a'>x</div><div class='cleanslate'><p>one</p>"
        "<div><b>two</b></div><p>three</p></div><div>after</div>"
    )
    assert div_inner(page, "class='cleanslate'") == (
        "<p>one</p><div><b>two</b></div><p>three</p>"
    )
    assert div_inner(page, "class='missing'") == ""


def test_oracle_pages_and_reads_each_posting(monkeypatch):
    seen = []

    def get(url, **kw):
        seen.append(kw.get("params", {}).get("finder", ""))
        if url.endswith("recruitingCEJobRequisitionDetails"):
            return FakeResp(
                {
                    "items": [
                        {
                            "ExternalDescriptionStr": "<p>Role</p>",
                            "ExternalQualificationsStr": "<p>Skills</p>",
                            "ExternalPostedStartDate": (
                                "2026-11-10T20:21:45+00:00"
                            ),
                        }
                    ]
                }
            )
        return FakeResp(
            {
                "items": [
                    {
                        "TotalJobsCount": 1,
                        "requisitionList": [
                            {
                                "Id": "2096",
                                "Title": "Analyst",
                                "PostedDate": "2026-11-10",
                                "PrimaryLocation": "Elyria, OH, United States",
                            }
                        ],
                    }
                ]
            }
        )

    monkeypatch.setattr(http, "get", get)
    [j] = VENDORS["oracle"].fetch("ccyj.fa.us6/CX")
    assert j.url == (
        "https://ccyj.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/"
        "sites/CX/job/2096"
    )
    assert j.description == "Role\n\nSkills"
    assert seen[0].startswith("findReqs;siteNumber=CX,limit=25,offset=0")
    assert seen[1] == 'ById;Id="2096",siteNumber=CX'
    key = VENDORS["oracle"].normalize_board(
        "https://CCYJ.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/"
        "sites/CX/jobs"
    )
    assert key == "ccyj.fa.us6/CX"


def test_eightfold(monkeypatch):
    def get(url, **kw):
        if url.endswith("/careers"):
            return html("<script>window._EF_GROUP_ID = 'bms.com';</script>")
        if "position_details" in url:
            return FakeResp(
                {"data": {"jobDescription": "<p>Lead <b>care</b></p>"}}
            )
        assert kw["params"]["domain"] == "bms.com"
        return FakeResp(
            {
                "data": {
                    "count": 1,
                    "positions": [
                        {
                            "id": "137",
                            "name": "Director",
                            "locations": ["Boston, MA"],
                            "workLocationOption": "remote",
                            "postedTs": 1794528000,
                            "positionUrl": "/careers/job/137",
                        }
                    ],
                }
            }
        )

    monkeypatch.setattr(http, "get", get)
    [j] = VENDORS["eightfold"].fetch("bms")
    assert j.url == "https://bms.eightfold.ai/careers/job/137"
    assert j.location == "Remote Boston, MA" and j.posted_at.startswith(
        "2026-"
    )
    assert j.description == "Lead care"


def test_loxo(monkeypatch):
    lst = (
        "<a class='job-title' style='x' href=\"/job/QUJD\">\n Admin\n</a>"
        "<div class='job-location'><i class='material-icons'>location_on</i>"
        " Tampa, Florida</div>"
    )
    page = "<div class='cleanslate' style='--x'><p>Great <b>job</b></p></div>"
    routes(
        monkeypatch, [("/job/QUJD", html(page)), ("app.loxo.co", html(lst))]
    )
    [j] = VENDORS["loxo"].fetch("Acme-Co")
    assert (j.title, j.url) == ("Admin", "https://app.loxo.co/job/QUJD")
    assert j.location == "Tampa, Florida" and j.description == "Great job"


def test_simplicant(monkeypatch):
    lst = (
        '<a class="list-group-item" '
        'href="//x.simplicant.com/jobs/62-eng/detail">'
        '<h3 class="list-group-item-title job-title">Engineer</h3>'
        '<div class="job-subtitle"> Alexandria, Virginia </div></a>'
        '<a class="list-group-item" href="/jobs/777-generic/apply">'
        '<h3 class="job-title">General</h3></a>'
    )
    routes(
        monkeypatch,
        [("/62-eng/detail", html(POSTING)), ("simplicant", html(lst))],
    )
    [j] = VENDORS["simplicant"].fetch("x")
    assert j.url == "https://x.simplicant.com/jobs/62-eng/detail"
    assert "pipes" in j.description


def test_peopleadmin_skips_the_empty_board_placeholder(monkeypatch):
    feed = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><id>https://x.peopleadmin.com/postings/19461</id>"
        "<published>2026-09-17T13:25:05-04:00</published>"
        '<link rel="alternate" href="https://x.peopleadmin.com/postings/19461"/>'
        "<title>Manager</title>"
        "<content>&lt;div&gt;Lead&lt;br&gt;the team&lt;/div&gt;"
        "</content><author><name>Dept</name></author></entry>"
        '<entry><link rel="alternate" href="https://x.peopleadmin.com/postings/search"/>'
        "<title>No Positions Currently Open</title></entry></feed>"
    )
    routes(monkeypatch, [("all_jobs.atom", html(feed))])
    [j] = VENDORS["peopleadmin"].fetch("x")
    assert (j.title, j.company) == ("Manager", "Dept")
    assert j.description == "Lead\nthe team"


def test_pageup(monkeypatch):
    lst = (
        '<tr><td><a class="job-link" '
        'href="/994/cw/en-us/job/1/welder">Welder</a>'
        '</td><td><span class="location">South Campus</span></td></tr>'
    )
    page = (
        '<div id="job"><div id="job-details">'
        "<p>Teach <b>welding</b></p></div></div>"
    )
    routes(
        monkeypatch, [("/job/1/welder", html(page)), ("/listing/", html(lst))]
    )
    [j] = VENDORS["pageup"].fetch("994/cw")
    assert j.location == "South Campus" and j.description == "Teach welding"
