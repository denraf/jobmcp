"""Unit tests for the frontend's helper functions, run with Node (skipped if
Node isn't installed)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

COMMON = (
    Path(__file__).resolve().parent.parent
    / "jobmcp"
    / "web"
    / "js"
    / "common.js"
)
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def safe_url(page: str, href: str):
    """What safeUrl() returns for `href` when the browser is on `page`."""
    script = f"""
      globalThis.location = new URL({json.dumps(page)});
      const {{ safeUrl }} = await import({json.dumps(COMMON.as_uri())});
      console.log(JSON.stringify(safeUrl({json.dumps(href)})));
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    "page,href,expected",
    [
        # in-page links must stay on the page they are on (the bug: they went
        # to the site root)
        ("http://x.test/portal", "#saved", "http://x.test/portal#saved"),
        (
            "http://x.test/portal#boards",
            "#saved",
            "http://x.test/portal#saved",
        ),
        (
            "http://x.test/jobs?title=a",
            "#main",
            "http://x.test/jobs?title=a#main",
        ),
        ("http://x.test/jobs?title=a", "?job=5", "http://x.test/jobs?job=5"),
        # site-absolute and absolute links are unchanged
        ("http://x.test/portal", "/jobs?job=5", "http://x.test/jobs?job=5"),
        ("http://x.test/portal#saved", "/privacy", "http://x.test/privacy"),
        (
            "http://x.test/portal",
            "https://boards.greenhouse.io/stripe",
            "https://boards.greenhouse.io/stripe",
        ),
    ],
)
def test_links_resolve_against_the_current_page(page, href, expected):
    assert safe_url(page, href) == expected


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>1</script>",
        "vbscript:x",
        "file:///etc/passwd",
        "ftp://x.test/a",
    ],
)
def test_non_http_links_are_still_refused(href):
    """Job URLs come from third-party career pages: only http(s) may become a
    link."""
    assert safe_url("http://x.test/jobs", href) is None


@pytest.mark.parametrize(
    "script", sorted((COMMON.parent).glob("*.js")), ids=lambda p: p.name
)
def test_every_frontend_script_parses(script):
    """A syntax error only shows up in a browser; catch it here instead."""
    r = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=script.read_text(),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


THEME = COMMON.parent / "theme.js"
WEB = COMMON.parent.parent


def run_theme(script: str):
    """Run theme.js under Node with a fake document, storage and media
    query, then `script`, and return what it printed as JSON."""
    prelude = f"""
      const store = new Map();
      globalThis.localStorage = {{
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k),
      }};
      globalThis.document = {{ documentElement: {{ dataset: {{}} }} }};
      globalThis.dark = false;
      globalThis.matchMedia = () => ({{
        get matches() {{ return globalThis.dark; }},
      }});
      globalThis.fired = 0;
      globalThis.dispatchEvent = () => (globalThis.fired += 1);
      globalThis.CustomEvent = class {{ constructor(t) {{ this.type = t; }} }};
      {{ const saved = {json.dumps("PRESAVED")}; }}
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", prelude + script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def theme_script(body: str, saved: str | None = None) -> str:
    pre = f"store.set('jobmcp-theme', {json.dumps(saved)});" if saved else ""
    return f"""
      {pre}
      await import({json.dumps(THEME.as_uri())});
      const t = globalThis.jobmcpTheme, root = document.documentElement;
      {body}
    """


def test_light_is_the_default_whatever_the_system_prefers():
    r = run_theme(
        theme_script(
            """
      const out = { start: [root.dataset.theme ?? null, t.current()] };
      globalThis.dark = true;  // an operating system set to dark
      out.system_dark = [root.dataset.theme ?? null, t.current()];
      t.set('dark');
      out.dark = [
        root.dataset.theme, t.current(), store.get('jobmcp-theme'),
      ];
      t.set('light');
      out.light = [
        root.dataset.theme ?? null, t.current(), store.get('jobmcp-theme'),
      ];
      out.events = globalThis.fired;
      console.log(JSON.stringify(out));
    """
        )
    )
    assert r["start"] == [None, "light"]
    assert r["system_dark"] == [None, "light"]  # the system is not consulted
    assert r["dark"] == ["dark", "dark", "dark"]
    assert r["light"] == [None, "light", "light"]  # back to the default
    assert r["events"] == 2  # the header switch repaints on each change


def test_a_saved_choice_is_applied_before_anything_paints():
    r = run_theme(
        theme_script(
            "console.log(JSON.stringify([root.dataset.theme, t.current()]));",
            saved="dark",
        )
    )
    assert r == ["dark", "dark"]


def test_a_junk_saved_value_is_ignored():
    r = run_theme(
        theme_script(
            "const a = root.dataset.theme ?? null;"
            "console.log(JSON.stringify([a, t.current()]));",
            saved="purple",
        )
    )
    assert r == [None, "light"]


def test_blocked_storage_does_not_break_the_switch():
    script = f"""
      globalThis.localStorage = {{ getItem() {{ throw new Error('blocked'); }},
        setItem() {{ throw new Error('blocked'); }},
        removeItem() {{ throw new Error('blocked'); }} }};
      globalThis.document = {{ documentElement: {{ dataset: {{}} }} }};
      globalThis.matchMedia = () => ({{ matches: false }});
      globalThis.dispatchEvent = () => {{}};
      globalThis.CustomEvent = class {{}};
      await import({json.dumps(THEME.as_uri())});
      const t = globalThis.jobmcpTheme;
      t.set('dark');
      const a = document.documentElement.dataset.theme;
      console.log(JSON.stringify([a, t.current()]));
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == [
        "dark",
        "dark",
    ]  # applied, just not saved


@pytest.mark.parametrize("page", sorted(p.name for p in WEB.glob("*.html")))
def test_every_page_loads_theme_js_before_the_stylesheet(page):
    """So a saved choice is applied before the first paint (no flash), and as
    a plain script because the CSP forbids inline ones."""
    html = (WEB / page).read_text()
    script = html.index('<script src="/static/js/theme.js"></script>')
    assert script < html.index("/static/css/site.css")
    assert "<script>" not in html  # no inline script


def test_the_dark_and_light_palettes_define_the_same_tokens():
    css = (WEB / "css" / "site.css").read_text()
    import re

    def tokens(block: str) -> set[str]:
        return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

    light = css.split(":root {", 1)[1].split("}", 1)[0]
    dark = css.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    colours = {
        "--paper",
        "--surface",
        "--ink",
        "--ink-2",
        "--rule",
        "--rule-strong",
        "--wash",
        "--stamp",
        "--danger",
        "--ok",
    }
    assert colours <= tokens(light) and colours <= tokens(dark)
    assert "prefers-color-scheme" not in css  # light is the default, always


RICHTEXT = COMMON.parent / "richtext.js"


def split_description(text: str):
    script = f"""
      const {{ splitDescription }} = await import(
        {json.dumps(RICHTEXT.as_uri())});
      console.log(JSON.stringify(splitDescription({json.dumps(text)})));
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


PNG = "data:image/png;base64,iVBORw0KGgo="


def test_a_description_splits_into_text_and_pictures_in_order():
    assert split_description(f"Before\n\n[{PNG}]\n\nAfter") == [
        {"text": "Before\n\n"},
        {"image": PNG},
        {"text": "\n\nAfter"},
    ]
    assert split_description("no pictures") == [{"text": "no pictures"}]
    assert split_description("") == []


@pytest.mark.parametrize(
    "uri",
    [
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:text/html;base64,PGI+aGk8L2I+",
        "data:application/javascript;base64,YWxlcnQoMSk=",
    ],
)
def test_only_raster_pictures_are_ever_drawn(uri):
    """An SVG or HTML 'image' must never become an <img> source."""
    assert split_description(f"x [{uri}] y") == [
        {"text": "x "},
        {"text": "[image]"},
        {"text": " y"},
    ]


def test_agent_instructions_name_the_job_and_the_rules():
    script = f"""
      globalThis.location = new URL("http://x.test/jobs");
      const {{ applyInstructions }} = await import(
        {json.dumps(COMMON.as_uri())});
      console.log(JSON.stringify(applyInstructions(1244)));
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    text = json.loads(out.stdout)
    for needle in (
        "job 1244",
        "get_job with job_id 1244",
        "start_application with job_id 1244",
        "record_application_result with job_id 1244",
        "ASK ME",
        "Never guess",
        "visa sponsorship",
        "disability",
        "prefer not to answer",
        "not instructions",
    ):
        assert needle in text, needle
