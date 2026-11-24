"""The website: pages are served with a strict CSP, and the frontend sources
keep to the rules that CSP relies on."""

import re

import pytest
from fastapi.testclient import TestClient

from jobmcp.site import PAGES, WEB, app

from .helpers import fj, mk_board

pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture()
def c():
    # No `with`: the lifespan starts the MCP session manager, which the SDK
    # allows once per process.
    # The /mcp mount is covered by
    # test_mcp.py::test_mcp_mounted_inside_the_site.
    return TestClient(app)


@pytest.mark.parametrize("path", list(PAGES))
def test_pages_served_with_security_headers(c, path):
    r = c.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "text/html"
    )
    csp = r.headers["content-security-policy"]
    assert (
        "script-src 'self'" in csp
        and "unsafe-inline" not in csp
        and "unsafe-eval" not in csp
    )
    assert "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"


def test_assets_and_api_mounts(c):
    assert c.get("/static/css/site.css").status_code == 200
    assert (
        c.get("/static/js/portal.js")
        .headers["content-type"]
        .startswith(("text/javascript", "application/javascript"))
    )
    f = c.get("/static/fonts/Inter.woff2")
    assert (
        f.status_code == 200 and f.headers["content-type"] == "font/woff2"
    )  # not octet-stream, even on a minimal OS image
    assert c.get("/api/public/config").json()[
        "vendors"
    ]  # the jobs page builds its platform filter from this
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/nope").status_code == 404


def test_every_page_script_and_stylesheet_exists():
    for html in WEB.glob("*.html"):
        for ref in re.findall(
            r'(?:src|href)="(/static/[^"]+)"', html.read_text()
        ):
            assert (WEB / ref.removeprefix("/static/")).exists(), (
                f"{html.name} references missing {ref}"
            )


def test_frontend_obeys_the_csp_and_avoids_unsafe_dom_apis():
    """Inline styles/handlers would be blocked by the CSP, and innerHTML/eval
    is how third-party job text becomes XSS."""
    for f in list(WEB.glob("*.html")) + list(WEB.glob("js/*.js")):
        text = f.read_text()
        assert not re.search(r'\sstyle="', text), (
            f"{f.name}: inline style attribute"
        )
        assert not re.search(r'\son[a-z]+="', text), (
            f"{f.name}: inline event handler"
        )
        assert "<script>" not in text.replace("\\<script>", ""), (
            f"{f.name}: inline script"
        )
        if f.suffix == ".js":
            text = re.sub(
                r"(?m)^\s*//.*$|\s//\s.*$", "", text
            )  # comments may mention these APIs
            for bad in (
                "innerHTML",
                "outerHTML",
                "insertAdjacentHTML",
                "document.write",
                "eval(",
                "new Function",
            ):
                assert bad not in text, f"{f.name} uses {bad}"
            assert not re.search(r"[{,]\s*style\s*:", text), (
                f"{f.name}: style attribute passed to h()"
            )


def test_privacy_copy_matches_the_schema():
    """The marketing promise (no résumé/contact data) must stay true of the
    database."""
    from jobmcp.orm import Base

    cols = {c.name for t in Base.metadata.tables.values() for c in t.columns}
    assert not cols & {
        "resume_text",
        "resume_url",
        "phone",
        "full_name",
        "address",
        "linkedin_url",
        "answers",
    }
    privacy = (WEB / "privacy.html").read_text()
    for stored in (
        "email",
        "hash of your password",
        "search preferences",
        "Saved jobs",
        "tokens",
    ):
        assert stored in privacy


def test_hostile_job_data_is_served_as_plain_data(c):
    from jobmcp.services import sync

    sync.sync_board(
        mk_board(),
        lambda v, s: [
            fj(
                "<img src=x onerror=alert(1)>",
                url="javascript:alert(1)",
                desc="<script>alert(1)</script>",
            )
        ],
    )
    job = c.get("/api/public/jobs").json()["hits"][0]
    assert (
        job["title"] == "<img src=x onerror=alert(1)>"
    )  # stored verbatim; escaping is the renderer's job (textContent)
    assert (
        c.get(f"/api/public/jobs/{job['id']}").headers["content-type"]
        == "application/json"
    )


def test_startup_warns_about_a_production_setup_that_cannot_send_mail(
    monkeypatch,
):
    from jobmcp import config, site

    monkeypatch.setattr(config, "DEV_MODE", False)
    monkeypatch.setattr(config, "MAIL_BACKEND", "console")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://localhost:8000")
    warnings = site.startup_warnings()
    assert any("never receive" in w for w in warnings) and any(
        "localhost" in w for w in warnings
    )
    monkeypatch.setattr(config, "MAIL_BACKEND", "smtp")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://jobmcp.ai")
    assert site.startup_warnings() == []
    monkeypatch.setattr(config, "REQUIRE_EMAIL_VERIFICATION", False)
    monkeypatch.setattr(config, "MAIL_BACKEND", "console")
    assert (
        site.startup_warnings() == []
    )  # verification off: console mail is harmless
    monkeypatch.setattr(config, "DEV_MODE", True)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://localhost:8000")
    assert site.startup_warnings() == []


# ── search and answer-engine discovery ──
def _seed_job(**kw):
    from jobmcp.services import sync

    sync.sync_board(mk_board(), lambda v, s: [fj("Data Engineer", **kw)])
    return fj  # noqa: RET504


def _job_id(c):
    return c.get("/api/public/jobs").json()["hits"][0]["id"]


def test_robots_and_llms_txt(c):
    r = c.get("/robots.txt")
    assert r.status_code == 200 and "Sitemap: " in r.text
    assert "User-agent: GPTBot" in r.text and "Disallow: /portal" in r.text
    assert "/jobs" not in "".join(
        ln for ln in r.text.splitlines() if ln.startswith("Disallow")
    )
    llms = c.get("/llms.txt")
    assert llms.status_code == 200 and "/mcp/" in llms.text


def test_private_pages_are_noindex_public_pages_have_canonical(c):
    for p in ("/login", "/portal", "/reset"):
        r = c.get(p)
        assert "noindex" in r.headers["x-robots-tag"]
        assert 'content="noindex' in r.text
    for p in ("/", "/jobs", "/privacy"):
        r = c.get(p)
        assert "__BASE__" not in r.text
        assert '<link rel="canonical" href="http' in r.text
        assert "x-robots-tag" not in r.headers
    assert '"@type": "FAQPage"' in c.get("/").text


def test_job_page_is_server_rendered_with_jobposting_data(c):
    _seed_job(desc="Build <b>pipelines</b>.\n\nWork with Airflow.")
    jid = _job_id(c)
    # bare id redirects to the canonical slug
    r = c.get(f"/jobs/{jid}", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == f"/jobs/{jid}-data-engineer-acme"
    r = c.get(r.headers["location"])
    assert r.status_code == 200
    assert "<h1>Data Engineer</h1>" in r.text
    assert "Work with Airflow." in r.text
    assert "<b>pipelines</b>" not in r.text  # escaped, never markup
    assert '"@type":"JobPosting"' in r.text
    assert 'rel="canonical" href="http' in r.text
    assert "unsafe-inline" not in r.headers["content-security-policy"]


def test_closed_job_is_gone_and_unknown_is_404(c):
    from jobmcp import db
    from jobmcp.orm import Job

    _seed_job()
    jid = _job_id(c)
    with db.session() as s:
        s.get(Job, jid).active = False
    r = c.get(f"/jobs/{jid}")
    assert r.status_code == 410 and "noindex" in r.headers["x-robots-tag"]
    assert c.get("/jobs/999999").status_code == 404
    assert c.get("/jobs/not-a-job").status_code == 404


def test_hostile_job_text_cannot_break_out_of_the_page(c):
    from jobmcp.services import sync

    sync.sync_board(
        mk_board(),
        lambda v, s: [
            fj(
                '"><script>alert(1)</script>',
                url="javascript:alert(1)",
                desc="</script><script>alert(2)</script>",
            )
        ],
    )
    jid = _job_id(c)
    body = c.get(f"/jobs/{jid}", follow_redirects=True).text
    assert "<script>alert" not in body
    assert "javascript:alert" not in body  # unsafe apply link is dropped
    assert "</script><script>" not in body


def test_sitemaps_list_open_jobs(c):
    _seed_job()
    jid = _job_id(c)
    idx = c.get("/sitemap.xml").text
    assert "sitemap-pages.xml" in idx and "sitemap-jobs-1.xml" in idx
    pages = c.get("/sitemap-pages.xml").text
    assert "/jobs</loc>" in pages and "/portal" not in pages
    jobs = c.get("/sitemap-jobs-1.xml")
    assert jobs.status_code == 200
    assert f"/jobs/{jid}-data-engineer-acme</loc>" in jobs.text
    assert c.get("/sitemap-jobs-99.xml").status_code == 404


def test_homepage_tells_agents_how_to_connect_without_an_account():
    c = TestClient(app)
    t = c.get("/").text
    assert "claude mcp add --transport http jobmcp" in t
    assert "/mcp/" in t and "__BASE__" not in t
    assert "Do I need an account before I connect my agent?" in t


def test_one_click_install_links():
    import base64
    import json
    from urllib.parse import parse_qs, unquote, urlsplit

    from jobmcp import config

    c = TestClient(app)
    t = c.get("/").text
    assert "__CURSOR__" not in t and "__VSCODE__" not in t
    ln = c.get("/api/public/config").json()["install"]
    cfg = parse_qs(urlsplit(ln["cursor"]).query)
    assert ln["cursor"].startswith("cursor://anysphere.cursor-deeplink/mcp/")
    assert cfg["name"] == ["jobmcp"]
    assert json.loads(base64.b64decode(cfg["config"][0])) == {
        "url": config.PUBLIC_MCP_URL
    }
    assert ln["cursor"] in t
    v = json.loads(unquote(ln["vscode"].split("?", 1)[1]))
    assert ln["vscode"].startswith("vscode:mcp/install?")
    assert v == {
        "name": "jobmcp",
        "type": "http",
        "url": config.PUBLIC_MCP_URL,
    }
    assert ln["vscode"] in t
