"""Job descriptions must arrive whole: no mid-sentence cut-off, and every part
of the posting (qualifications included)."""

import html as html_lib

import pytest
from sqlalchemy import select

from jobmcp import db
from jobmcp.models import (
    MAX_DESCRIPTION,
    TRUNCATION_NOTE,
    clean_html,
    fit,
    normalize_text,
    split_images,
    strip_data_uris,
)
from jobmcp.orm import Job
from jobmcp.services import sync
from jobmcp.vendors import VENDORS

from .helpers import fj, mk_board
from .test_vendors import FakeResp, patch

# the sentence that used to be cut off
LAST = "We look forward to hearing from you, whatever your background."


def long_html(paragraphs=120):
    """A posting well over the old 4,000-character cap, ending in a known
    sentence."""
    body = "".join(
        f"<p>Responsibility number {i}: we expect you to own your area "
        "end to end and keep it healthy.</p>"
        for i in range(paragraphs)
    )
    return (
        f"<p>About the role</p>{body}<h3>Qualifications</h3>"
        "<ul><li>5+ years of Python</li><li>SQL</li></ul>"
        f"<p>{LAST}</p>"
    )


# ── the cleaner ─────────────────────────────────────────────────────────
def test_clean_html_never_shortens():
    text = clean_html(long_html())
    assert len(text) > 10_000 and text.endswith(LAST)
    assert clean_html("<p>" + "word " * 100_000 + "end.</p>").endswith(
        "end."
    )  # 500 KB in, 500 KB out


def test_clean_html_keeps_paragraphs_and_bullets_readable():
    out = clean_html(
        "<h2>Qualifications</h2><ul><li>5+ years of Python</li><li>SQL &amp; "
        "data modelling</li></ul>"
        "<p>Nice to have: Go<br>or Rust</p>"
    )
    assert (
        out == "Qualifications\n\n- 5+ years of Python\n- SQL & data "
        "modelling\n\nNice to have: Go\nor Rust"
    )
    assert (
        clean_html("<p>First para.</p><p>Second para.</p>")
        == "First para.\n\nSecond para."
    )


def test_clean_html_handles_greenhouses_double_escaping_and_entities():
    escaped = html_lib.escape(
        "<p>Rock &amp; roll&nbsp;engineer</p><ul><li>Item</li></ul>"
    )  # Greenhouse escapes the HTML itself
    assert clean_html(escaped) == "Rock & roll engineer\n\n- Item"
    assert clean_html(None) == "" and clean_html("") == ""


def test_normalize_text_keeps_line_structure():
    assert normalize_text("  a   b \n\n\n\n c\t d  ") == "a b\n\nc d"


def test_the_safety_limit_is_visible_never_silent():
    assert fit("x" * 1000) == "x" * 1000
    big = fit("sentence. " * 50_000)
    assert len(big) <= MAX_DESCRIPTION and big.endswith(
        TRUNCATION_NOTE
    )  # says so, rather than stopping mid-sentence
    assert (
        MAX_DESCRIPTION > 10 * 15_000
    )  # far above the longest real posting (~15k characters)


# ── each platform ───────────────────────────────────────────────────────
def test_greenhouse_description_is_whole(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            {
                "jobs": [
                    {
                        "title": "T",
                        "absolute_url": "u",
                        "company_name": "Acme",
                        "location": {"name": "Remote"},
                        "content": html_lib.escape(long_html()),
                    }
                ]
            }
        ),
    )
    [j] = VENDORS["greenhouse"].fetch("acme")
    assert len(j.description) > 10_000 and j.description.endswith(LAST)
    assert (
        "Qualifications" in j.description
        and "- 5+ years of Python" in j.description
    )


def test_ashby_description_is_whole(monkeypatch):
    plain = (
        "Intro.\n\n"
        + "\n".join(
            f"Responsibility {i}: " + "detail " * 20 for i in range(100)
        )
        + f"\n\n{LAST}"
    )
    patch(
        monkeypatch,
        FakeResp(
            {
                "jobs": [
                    {
                        "title": "T",
                        "isListed": True,
                        "jobUrl": "u",
                        "descriptionPlain": plain,
                    }
                ]
            }
        ),
    )
    [j] = VENDORS["ashby"].fetch("acme")
    assert (
        len(j.description) > 10_000
        and j.description.endswith(LAST)
        and "\n" in j.description
    )


def test_lever_includes_the_lists_where_the_qualifications_live(monkeypatch):
    """Lever's descriptionPlain is only the introduction; 'Who You Are' etc.
    come in `lists`, the close in `additional`."""
    posting = {
        "text": "Engineer",
        "hostedUrl": "https://x",
        "categories": {"location": "Remote"},
        "openingPlain": "Join us.",
        "descriptionPlain": "We build things.",
        "lists": [
            {
                "text": "What You'll Do",
                "content": "<li>Build services</li><li>Review code</li>",
            },
            {
                "text": "Who You Are",
                "content": "<li>5+ years of Python</li><li>SQL</li>",
            },
        ],
        "additionalPlain": LAST,
    }
    patch(monkeypatch, FakeResp([posting]))
    [j] = VENDORS["lever"].fetch("acme")
    d = j.description
    assert (
        d.index("Join us.")
        < d.index("We build things.")
        < d.index("What You'll Do")
        < d.index("Who You Are")
        < d.index(LAST)
    )
    assert (
        "- 5+ years of Python" in d and "- SQL" in d and d.endswith(LAST)
    )  # the qualifications are there, as a list


def test_lever_with_no_lists_or_extras_still_works(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            [
                {
                    "text": "SRE",
                    "hostedUrl": "u",
                    "categories": {},
                    "descriptionPlain": "hi",
                }
            ]
        ),
    )
    assert VENDORS["lever"].fetch("acme")[0].description == "hi"


def test_recruitee_includes_the_requirements_field(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            {
                "offers": [
                    {
                        "title": "T",
                        "careers_url": "u",
                        "status": "published",
                        "description": "<p>The role.</p>",
                        "requirements": (
                            "<ul><li>5+ years of Python</li></ul><p>"
                        )
                        + LAST
                        + "</p>",
                    }
                ]
            }
        ),
    )
    [j] = VENDORS["recruitee"].fetch("acme")
    assert (
        j.description.startswith("The role.")
        and "Requirements\n- 5+ years of Python" in j.description
        and j.description.endswith(LAST)
    )


def test_workable_teamtailor_personio_are_whole(monkeypatch):
    patch(
        monkeypatch,
        FakeResp(
            {
                "name": "Acme",
                "jobs": [
                    {"title": "T", "url": "u", "description": long_html()}
                ],
            }
        ),
    )
    assert VENDORS["workable"].fetch("acme")[0].description.endswith(LAST)
    rss = (
        "<rss "
        "xmlns:tt='https://teamtailor.com/locations'><channel><item><title>T</title><link>u</link>"
        f"<description>{html_lib.escape(long_html())}</description></item></channel></rss>"
    ).encode()
    patch(monkeypatch, FakeResp(content=rss))
    d = VENDORS["teamtailor"].fetch("acme")[0].description
    assert len(d) > 10_000 and d.endswith(LAST)
    xml = (
        "<workzag-jobs><position><id>9</id><name>Dev</name><office>Munich</office><createdAt>2026-10-01T00:00:00+00:00</createdAt>"
        "<jobDescriptions>"
        "<jobDescription><name>Your tasks</name><value>"
        f"{html_lib.escape('<p>Build things.</p>')}"
        "</value></jobDescription>"
        "<jobDescription><name>Your profile</name><value>"
        f"{html_lib.escape(long_html(40))}"
        "</value></jobDescription>"
        "</jobDescriptions></position></workzag-jobs>"
    ).encode()
    patch(monkeypatch, FakeResp(content=xml))
    d = VENDORS["personio"].fetch("acme.de")[0].description
    assert (
        "Your tasks\nBuild things." in d
        and "\n\nYour profile\n" in d
        and d.endswith(LAST)
    )  # sections stay separate


# ── stored, and served ──────────────────────────────────────────────────
@pytest.mark.usefixtures("fresh_db")
class TestStoredAndServed:
    def test_a_long_description_is_stored_whole_in_mysql(self):
        long = (
            "Qualifications: " + "knows things. " * 10_000 + LAST
        )  # ~140 KB: past TEXT's 64 KB, under the safety limit
        sync.sync_board(mk_board(), lambda v, s: [fj("T", desc=long)])
        with db.session() as s:
            assert s.scalar(select(Job.description)) == long

    def test_a_previously_truncated_row_is_repaired_by_the_next_sync(self):
        bid = mk_board()
        sync.sync_board(
            bid,
            lambda v, s: [fj("T", desc="Qualifications: 5+ years of Pyth")],
        )  # what the old cap stored
        full = "Qualifications: 5+ years of Python. " + LAST
        sync.sync_board(bid, lambda v, s: [fj("T", desc=full)])
        with db.session() as s:
            assert s.scalar(select(Job.description)) == full
            assert (
                len(list(s.scalars(select(Job)))) == 1
            )  # the same job, updated in place

    def test_force_resync_refetches_boards_that_are_not_due(self):
        bid = mk_board()
        sync.sync_board(bid, lambda v, s: [fj("T", desc="short")])
        assert sync.claim_due(10) == []  # not due for ~an hour
        res = sync.run_due(
            2,
            fetcher=lambda v, s: [fj("T", desc="the whole description")],
            until_empty=True,
            force=True,
        )
        assert len(res) == 1 and res[0]["ok"]
        with db.session() as s:
            assert s.scalar(select(Job.description)) == "the whole description"

    def test_cli_sync_once_all(self, monkeypatch, capsys):
        from jobmcp import cli

        bid = mk_board()
        sync.sync_board(bid, lambda v, s: [fj("T", desc="short")])
        monkeypatch.setattr(
            sync,
            "default_fetcher",
            lambda v, b: [fj("T", board=b, desc="full text now")],
        )
        monkeypatch.setattr(db, "init", lambda url=None: db.engine())
        cli.main(["sync-once"])
        assert "synced 0 boards" in capsys.readouterr().out
        cli.main(["sync-once", "--all"])
        assert "synced 1 boards" in capsys.readouterr().out
        with db.session() as s:
            assert s.scalar(select(Job.description)) == "full text now"

    def test_the_api_returns_the_whole_description(self):
        from fastapi.testclient import TestClient

        from jobmcp.api import app

        long = "Qualifications: " + "knows things. " * 2000 + LAST
        sync.sync_board(mk_board(), lambda v, s: [fj("T", desc=long)])
        c = TestClient(app)
        jid = c.get("/public/jobs").json()["hits"][0]["id"]
        d = c.get(f"/public/jobs/{jid}").json()["description"]
        assert d == long and d.endswith(LAST)


@pytest.mark.usefixtures("fresh_db")
def test_a_big_board_is_stored_in_slices_and_resynced_correctly(monkeypatch):
    """More postings than one flush slice: new, unchanged, updated and closed
    postings must all come out right, and a fetch with no description must
    not wipe the stored one."""
    monkeypatch.setattr(sync, "FLUSH_EVERY", 7)
    bid = mk_board("lever", "big")
    first = [
        fj(f"Role {i}", board="big", vendor="lever", desc=f"d{i}")
        for i in range(30)
    ]
    res = sync.sync_board(bid, lambda v, b: first)
    assert res["jobs"] == 30 and res["new"] == 30
    second = [
        fj(
            f"Role {i}",
            board="big",
            vendor="lever",
            desc="" if i == 0 else f"changed{i}",
        )
        for i in range(25)
    ]
    res = sync.sync_board(bid, lambda v, b: second)
    assert res["jobs"] == 25 and res["new"] == 0 and res["closed"] == 5
    with db.session() as s:
        rows = {j.title: j for j in s.scalars(select(Job))}
    assert rows["Role 0"].description == "d0"  # empty fetch keeps the old text
    assert rows["Role 1"].description == "changed1"
    assert rows["Role 29"].active is False and rows["Role 24"].active is True


IMG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"


def test_an_embedded_image_is_kept_and_the_text_after_it_too():
    """Real Ashby posting: descriptionPlain held a 298,000 character
    `[data:image/png;base64,...]`. It is kept (the site draws it), and the
    size limit counts text only, so the words after the picture survive."""
    image = "iVBORw0KGgo" + "AAAANSUhEUgAAB" * 22000  # about 300,000 chars
    plain = (
        "Hi, I am the CTO.\n\nHere is a calendar of an engineer:\n\n"
        f"[data:image/png;base64,{image}]\n\n"
        "Things you will do:\n- Lead the platform team\n- Keep it reliable\n\n"
        "LAST SENTENCE OF THE POSTING."
    )
    assert len(plain) > MAX_DESCRIPTION  # over the old limit, by picture alone
    out = fit(normalize_text(plain))
    assert f"[data:image/png;base64,{image}]" in out  # the picture is intact
    assert out.endswith("LAST SENTENCE OF THE POSTING.")
    assert "shortened" not in out
    assert strip_data_uris(out).count("[image]") == 1


@pytest.mark.parametrize(
    "text,want",
    [
        (f"see {IMG} here", f"see [{IMG}] here"),  # a bare one gets brackets
        (f"[{IMG}]", f"[{IMG}]"),
        ("[data:image/svg+xml;base64,PHN2Zz4=]", "[image]"),  # not a picture
        ("[data:text/html;base64,PGI+aGk8L2I+]", "[image]"),
        (
            "a [link](https://x.io/data) stays",
            "a [link](https://x.io/data) stays",
        ),
        ("data: not an image", "data: not an image"),
    ],
)
def test_only_real_picture_formats_are_kept(text, want):
    assert normalize_text(text) == want


def test_html_images_with_data_uris_are_kept_as_pictures():
    out = clean_html(
        f'<p>Before</p><p><img style="max-width:100%" src="{IMG}"></p>'
        "<p>After</p>"
    )
    assert out == f"Before\n\n[{IMG}]\n\nAfter"
    assert (
        clean_html('<p>x</p><img src="https://x.io/a.png">') == "x"
    )  # linked


def test_a_huge_picture_or_too_many_become_a_marker_not_a_cut():
    from jobmcp import models

    big = "data:image/png;base64," + "A" * (models.MAX_IMAGE + 10)
    assert normalize_text(f"a [{big}] b") == "a [image] b"
    many = " ".join(
        f"[data:image/png;base64,{'B' * 1_900_000}]" for _ in range(6)
    )  # six allowed pictures, but the eight-million budget fits only four
    out = fit(normalize_text(many))
    assert (
        out.count("[data:image/png;base64,") == 4 and out.count("[image]") == 2
    )


def test_text_over_the_limit_is_still_shortened_visibly_and_drops_pictures():
    long_text = "word " * 50_000  # 250,000 characters of real text
    out = fit(f"[{IMG}]\n\n{long_text}")
    assert "shortened" in out and "data:image" not in out


def test_agents_and_search_see_text_not_picture_data():
    text = f"Intro\n\n[{IMG}]\n\nOutro"
    assert strip_data_uris(text) == "Intro\n\n[image]\n\nOutro"
    assert strip_data_uris(None) == "" and strip_data_uris("plain") == "plain"
    assert split_images(text) == [
        ("text", "Intro\n\n"),
        ("image", IMG),
        ("text", "\n\nOutro"),
    ]


def test_ashby_parser_keeps_the_picture(monkeypatch):
    from .test_vendors import FakeResp, patch

    image = "A" * 250_000
    patch(
        monkeypatch,
        FakeResp(
            {
                "jobs": [
                    {
                        "title": "Head of Platform",
                        "location": "Remote",
                        "jobUrl": "https://jobs.ashbyhq.com/Ashby/x",
                        "publishedAt": "2026-11-01T00:00:00Z",
                        "descriptionPlain": (
                            f"Intro\n\n[data:image/png;base64,{image}]\n\n"
                            "The end."
                        ),
                    }
                ]
            }
        ),
    )
    [j] = VENDORS["ashby"].fetch("Ashby")
    assert j.description == (
        f"Intro\n\n[data:image/png;base64,{image}]\n\nThe end."
    )


@pytest.mark.usefixtures("fresh_db")
def test_stored_whole_served_to_the_site_but_not_to_agents_or_search():
    from sqlalchemy import select as sel

    from jobmcp import mcp_server
    from jobmcp.services import search

    bid = mk_board("ashby", "Acme")
    desc = (
        f"We use python daily.\n\n[data:image/png;base64,{'A' * 5000}]\n\nEnd."
    )
    sync.sync_board(
        bid, lambda v, s: [fj("Eng", board=s, vendor=v, desc=desc)]
    )
    with db.session() as s:
        j = s.scalar(sel(Job))
        assert (
            f"base64,{'A' * 5000}" in j.description
        )  # stored with the picture
        # the website's API returns it whole
        from jobmcp.api import job_out

        assert "base64," in job_out(j, full=True)["description"]
        # an agent gets text only, and a small reply
        agent = mcp_server._job(j, full=True)["description"]
        assert (
            "base64" not in agent and "[image]" in agent and len(agent) < 100
        )
        # a keyword inside the picture's data is not a search hit
        junk = j.description.replace(
            "A" * 5000, "A" * 100 + "zebra" + "A" * 100
        )
        j.description = junk
        s.flush()
        hit = search.search_jobs(s, None, keywords="zebra")
        assert hit["total"] == 0
        hit = search.search_jobs(s, None, keywords="python")
        assert hit["total"] == 1 and "base64" not in hit["hits"][0].snippet
