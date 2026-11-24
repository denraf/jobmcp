# ruff: noqa: E501  (the HTML and llms.txt templates below are long prose lines)
"""Search and answer-engine discovery: robots.txt, sitemaps, llms.txt, and a
server-rendered page (with schema.org JobPosting data) for every open job.

The jobs list on /jobs is drawn by script, which a crawler may not run, so each
job also has its own plain-HTML page at /jobs/{id}-{slug}. Those pages need no
script at all: Google, Bing, ChatGPT search and the other agents can read them
as they are."""

from __future__ import annotations

import html
import json
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import func, select

from . import config, db
from .orm import Board, Job

router = APIRouter()

SITEMAP_CHUNK = 20_000  # the protocol allows 50,000 URLs per file
SITEMAP_TTL = 3600
SITE_NAME = "jobmcp"
TAGLINE = "Your agent applies. Your résumé stays yours."
# Pages a person signs in to, or that only make sense with a token in the URL.
# Kept out of search results, and out of crawls.
PRIVATE_PAGES = (
    "/login",
    "/register",
    "/portal",
    "/verify",
    "/forgot",
    "/reset",
    "/oauth/authorize",
)
# Crawlers of the search engines and of the AI assistants. Named one by one so
# the intent is on record; each group repeats the rules, because a crawler that
# matches a named group ignores the `*` group.
CRAWLERS = (
    "*",
    "Googlebot",
    "Bingbot",
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-SearchBot",
    "Claude-User",
    "PerplexityBot",
    "Perplexity-User",
    "Google-Extended",
    "Applebot",
    "Applebot-Extended",
    "Meta-ExternalAgent",
    "DuckAssistBot",
    "CCBot",
)

_DATA = re.compile(
    r"\[?[ \t]*data:[\w.+/-]+;base64,[A-Za-z0-9+/=_-]+[ \t]*\]?"
)
_PARAS = re.compile(r"\n\s*\n")
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def base() -> str:
    return config.PUBLIC_BASE_URL


def slugify(*parts: str) -> str:
    text = " ".join(p for p in parts if p)
    text = (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80].strip("-")


def job_path(j: Job) -> str:
    slug = slugify(j.title, j.company)
    return f"/jobs/{j.id}-{slug}" if slug else f"/jobs/{j.id}"


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", _DATA.sub(" ", text or "")).strip()


def _snippet(text: str, n: int = 155) -> str:
    t = _plain(text)
    if len(t) <= n:
        return t
    return t[: n - 1].rsplit(" ", 1)[0].rstrip(".,;:- ") + "…"


def _safe_http(url: str) -> str | None:
    return url if re.match(r"https?://", url or "", re.I) else None


def _ld(data: dict) -> str:
    """JSON for a <script type="application/ld+json"> block. `<` is escaped so
    that job text can never close the tag."""
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace("\u2028", "\\u2028")


def _iso(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%d") if d else None


def head(
    title: str,
    description: str,
    path: str,
    *,
    robots: str | None = None,
    og_type: str = "website",
    ld: list[dict] | None = None,
) -> str:
    e = html.escape
    url = base() + path
    out = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(description, quote=True)}">',
        f'<link rel="canonical" href="{e(url, quote=True)}">',
    ]
    if robots:
        out.append(f'<meta name="robots" content="{robots}">')
    out += [
        f'<meta property="og:site_name" content="{SITE_NAME}">',
        f'<meta property="og:type" content="{og_type}">',
        f'<meta property="og:title" content="{e(title, quote=True)}">',
        f'<meta property="og:description" content="{e(description, quote=True)}">',
        f'<meta property="og:url" content="{e(url, quote=True)}">',
        '<meta name="twitter:card" content="summary">',
        f'<meta name="twitter:title" content="{e(title, quote=True)}">',
        f'<meta name="twitter:description" content="{e(description, quote=True)}">',
    ]
    for block in ld or []:
        out.append(f'<script type="application/ld+json">{_ld(block)}</script>')
    return "\n".join(out)


# ── job pages ───────────────────────────────────────────────────────────
LEVEL = {
    "intern": "Intern",
    "entry": "Entry level",
    "senior": "Senior",
    "lead": "Lead or manager",
    "executive": "Director and above",
}


def _description_html(text: str) -> str:
    text = _DATA.sub("", text or "").strip()
    if not text:
        return (
            "<p>The company hasn’t shared a description here. Open the "
            "company’s page for details.</p>"
        )
    paras = [p.strip() for p in _PARAS.split(text) if p.strip()]
    return "\n".join(
        "<p>" + "<br>\n".join(html.escape(ln) for ln in p.split("\n")) + "</p>"
        for p in paras
    )


def job_posting_ld(j: Job) -> dict:
    ld: dict = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": j.title,
        "description": _description_html(j.description),
        "datePosted": _iso(j.posted_at or j.first_seen),
        "identifier": {
            "@type": "PropertyValue",
            "name": SITE_NAME,
            "value": str(j.id),
        },
        "hiringOrganization": {"@type": "Organization", "name": j.company},
        "url": base() + job_path(j),
        "directApply": False,
    }
    country = j.country or ""
    if j.workplace == "remote":
        ld["jobLocationType"] = "TELECOMMUTE"
        if country:
            ld["applicantLocationRequirements"] = {
                "@type": "Country",
                "name": country,
            }
    if j.location or country:
        addr: dict = {"@type": "PostalAddress"}
        if j.location:
            addr["addressLocality"] = j.location
        if country:
            addr["addressCountry"] = country
        ld["jobLocation"] = {"@type": "Place", "address": addr}
    if j.salary_min or j.salary_max:
        val: dict = {"@type": "QuantitativeValue", "unitText": "YEAR"}
        if j.salary_min:
            val["minValue"] = j.salary_min
        if j.salary_max:
            val["maxValue"] = j.salary_max
        ld["baseSalary"] = {
            "@type": "MonetaryAmount",
            "currency": j.salary_currency or "USD",
            "value": val,
        }
    return {k: v for k, v in ld.items() if v}


def render_job(j: Job) -> str:
    from .salary import format_salary

    e = html.escape
    where = j.location or ""
    title = f"{j.title} at {j.company}" if j.company else j.title
    if where:
        title += f", {where}"
    title = title[:110] + f" | {SITE_NAME}"
    desc = _snippet(j.description) or (
        f"{j.title} at {j.company or 'a company'}"
        + (f" in {where}" if where else "")
        + ". Open role from the employer’s own career page."
    )
    pay = format_salary(
        j.salary_min, j.salary_max, j.salary_currency, j.salary_period
    )
    chips = [
        c
        for c in (
            j.company,
            where,
            pay,
            {"remote": "Remote", "hybrid": "Hybrid"}.get(j.workplace or ""),
            LEVEL.get(j.seniority or ""),
            "Posted " + _iso(j.posted_at) if j.posted_at else None,
        )
        if c
    ]
    apply = _safe_http(j.url)
    crumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "Jobs",
                "item": base() + "/jobs",
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": j.title,
                "item": base() + job_path(j),
            },
        ],
    }
    h = head(
        title,
        desc,
        job_path(j),
        og_type="article",
        ld=[job_posting_ld(j), crumbs],
    )
    return f"""<!doctype html>
<html lang="en">
<head>
{h}
<meta name="color-scheme" content="light">
<link rel="preload" href="/static/fonts/Inter.woff2" as="font" type="font/woff2" crossorigin>
<script src="/static/js/theme.js"></script>
<link rel="stylesheet" href="/static/css/site.css">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='4' fill='%23f4dc5a'/%3E%3Cpath d='M9 8v16h4V8z M17 14h6v10h-6z' fill='%230f2530'/%3E%3C/svg%3E">
</head>
<body>
<header class="site-head" id="site-head"></header>
<main id="main">
<div class="wrap">
  <article class="prose">
    <p class="small muted ui"><a href="/jobs">All jobs</a></p>
    <h1>{e(j.title)}</h1>
    <div class="job-meta ui">{"".join(f"<span>{e(c)}</span>" for c in chips)}</div>
    {f'<p><a class="btn btn-stamp" href="{e(apply, quote=True)}" target="_blank" rel="nofollow noopener noreferrer">Open the company’s page</a></p>' if apply else ""}
    <h2>About the role</h2>
    {_description_html(j.description)}
    <p class="detail-note">jobmcp collects open roles from employers’ own career pages and hands them to your own AI agent. It never applies for you. <a href="/">How it works</a>.</p>
  </article>
</div>
</main>
<footer class="site-foot" id="site-foot"></footer>
<script type="module" src="/static/js/job.js"></script>
</body>
</html>
"""


def _gone_page(status_title: str) -> str:
    h = head(
        f"{status_title} | {SITE_NAME}",
        "This job is no longer open.",
        "/jobs",
        robots="noindex",
    )
    return f"""<!doctype html>
<html lang="en">
<head>
{h}
<link rel="stylesheet" href="/static/css/site.css">
<script src="/static/js/theme.js"></script>
</head>
<body>
<header class="site-head" id="site-head"></header>
<main id="main"><div class="wrap"><div class="page-head"><h1>{html.escape(status_title)}</h1>
<p class="muted">Companies close roles quickly. <a href="/jobs">Browse the jobs that are open now</a>.</p></div></div></main>
<footer class="site-foot" id="site-foot"></footer>
<script type="module" src="/static/js/job.js"></script>
</body>
</html>
"""


@router.get("/jobs/{ref}", include_in_schema=False)
def job_page(ref: str):
    m = re.match(r"^(\d{1,12})(?:-.*)?$", ref)
    if not m:
        raise HTTPException(404)
    with db.session() as s:
        j = s.get(Job, int(m.group(1)))
        if j is None:
            raise HTTPException(404)
        if not j.active or j.board.status != "enabled":
            # Gone for good: tells a search engine to drop it from the index.
            return Response(
                _gone_page("This job is no longer open"),
                status_code=410,
                media_type="text/html",
                headers={"X-Robots-Tag": "noindex"},
            )
        want = job_path(j)
        if "/jobs/" + ref != want:
            return RedirectResponse(want, status_code=301)
        body = render_job(j)
    return Response(
        body,
        media_type="text/html",
        headers={"Cache-Control": "public, max-age=300"},
    )


# ── robots.txt, llms.txt ────────────────────────────────────────────────
def robots_txt() -> str:
    rules = [
        "Allow: /",
        "Disallow: /oauth/",
        "Disallow: /api/",
        "Disallow: /mcp",
        "Disallow: /metrics",
    ]
    rules += [f"Disallow: {p}" for p in PRIVATE_PAGES]
    groups = []
    for ua in CRAWLERS:
        groups.append(f"User-agent: {ua}\n" + "\n".join(rules))
    return "\n\n".join(groups) + f"\n\nSitemap: {base()}/sitemap.xml\n"


def llms_txt() -> str:
    b = base()
    return f"""# {SITE_NAME}

> {SITE_NAME} collects open jobs from employers' own career pages (Greenhouse, Lever, Ashby, Workable, Workday and many more hiring platforms) and hands them to a job seeker's own AI agent over MCP. It never applies for anyone and never stores a résumé, phone number or address. The agent does the applying, on the company's own form, with details the user keeps on their side.

## For people and agents reading this site

- [Home]({b}/): what {SITE_NAME} does and doesn't do, how it works, and common questions.
- [Browse jobs]({b}/jobs): search open roles by title, keyword, location, remote, seniority, salary and hiring platform. No account needed.
- [What we store]({b}/privacy): exactly what data {SITE_NAME} holds about a user.
- [Sitemap]({b}/sitemap.xml): every open job has its own page at {b}/jobs/<id>-<slug>.

## Connecting an agent

- MCP endpoint (streamable HTTP): {config.PUBLIC_MCP_URL}
- Authentication: OAuth sign-in (the agent discovers it from the endpoint). The user enters their email on the approval page, opens the emailed link, which creates a free account if needed, and approves. A personal token from the portal also works. Works with Claude, Codex, Gemini CLI and any assistant that can connect to an MCP server.
- Tools search jobs, show a posting, claim a job, and record whether it was applied to, skipped or failed.

## Optional

- [Sign up]({b}/register)
"""


@router.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse(
        robots_txt(), headers={"Cache-Control": "public, max-age=3600"}
    )


@router.get("/llms.txt", include_in_schema=False)
def llms():
    return PlainTextResponse(
        llms_txt(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ── sitemaps ────────────────────────────────────────────────────────────
def _cached(key: str, build) -> str:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < SITEMAP_TTL:
            return hit[1]
    body = build()
    with _cache_lock:
        _cache[key] = (now, body)
    return body


def _xml(body: str) -> Response:
    return Response(
        body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _open_jobs():
    return (
        Job.active == 1,
        Job.board_id.in_(select(Board.id).where(Board.status == "enabled")),
    )


def _index() -> str:
    with db.session() as s:
        n = s.scalar(
            select(func.count()).select_from(Job).where(*_open_jobs())
        )
    chunks = max(1, -(-(n or 0) // SITEMAP_CHUNK)) if n else 0
    urls = [f"{base()}/sitemap-pages.xml"] + [
        f"{base()}/sitemap-jobs-{i}.xml" for i in range(1, chunks + 1)
    ]
    items = "".join(
        f"<sitemap><loc>{html.escape(u)}</loc></sitemap>" for u in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{items}</sitemapindex>\n"
    )


def _pages() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = [
        ("/", "weekly", "1.0"),
        ("/jobs", "hourly", "0.9"),
        ("/privacy", "yearly", "0.3"),
    ]
    items = "".join(
        f"<url><loc>{html.escape(base() + p)}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{f}</changefreq><priority>{pr}</priority></url>"
        for p, f, pr in rows
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{items}</urlset>\n"
    )


def _jobs(chunk: int) -> str:
    with db.session() as s:
        rows = s.execute(
            select(Job.id, Job.title, Job.company, Job.sort_at)
            .where(*_open_jobs())
            .order_by(Job.id)
            .offset((chunk - 1) * SITEMAP_CHUNK)
            .limit(SITEMAP_CHUNK)
        ).all()
    items = []
    for id_, title, company, at in rows:
        slug = slugify(title, company)
        path = f"/jobs/{id_}-{slug}" if slug else f"/jobs/{id_}"
        items.append(
            f"<url><loc>{html.escape(base() + path)}</loc>"
            f"<lastmod>{_iso(at) or ''}</lastmod></url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(items)
        + "</urlset>\n"
    )


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_index():
    return _xml(_cached("index", _index))


@router.get("/sitemap-pages.xml", include_in_schema=False)
def sitemap_pages():
    return _xml(_cached("pages", _pages))


@router.get("/sitemap-jobs-{chunk}.xml", include_in_schema=False)
def sitemap_jobs(chunk: int):
    if not 1 <= chunk <= 100:  # bounds the cache
        raise HTTPException(404)
    body = _cached(f"jobs-{chunk}", lambda: _jobs(chunk))
    if "<url>" not in body:
        raise HTTPException(404)
    return _xml(body)
