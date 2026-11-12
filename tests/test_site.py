"""The website: pages are served with a strict CSP, and the frontend sources
keep to the rules that CSP relies on."""

import re

import pytest
from fastapi.testclient import TestClient

from livejobs.site import PAGES, WEB, app

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
    from livejobs.orm import Base

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
    from livejobs.services import sync

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
    from livejobs import config, site

    monkeypatch.setattr(config, "DEV_MODE", False)
    monkeypatch.setattr(config, "MAIL_BACKEND", "console")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://localhost:8000")
    warnings = site.startup_warnings()
    assert any("never receive" in w for w in warnings) and any(
        "localhost" in w for w in warnings
    )
    monkeypatch.setattr(config, "MAIL_BACKEND", "smtp")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://jobs.example.com")
    assert site.startup_warnings() == []
    monkeypatch.setattr(config, "REQUIRE_EMAIL_VERIFICATION", False)
    monkeypatch.setattr(config, "MAIL_BACKEND", "console")
    assert (
        site.startup_warnings() == []
    )  # verification off: console mail is harmless
    monkeypatch.setattr(config, "DEV_MODE", True)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://localhost:8000")
    assert site.startup_warnings() == []
