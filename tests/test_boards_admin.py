"""Manual board management (admin only) and the monthly Common Crawl
refresh."""

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from livejobs import cli, db, inventory, metrics, migrations
from livejobs.api import app
from livejobs.orm import Board, InventoryRun, Job
from livejobs.services import ServiceError, boards, sync
from livejobs.vendors import VENDORS
from livejobs.vendors.base import BoardGone

from .helpers import fj, mk_board, mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")
PW = "correct-horse-battery"


@pytest.fixture()
def c():
    return TestClient(app)


def login(c, email):
    return {
        "Authorization": "Bearer "
        + c.post("/auth/login", json={"email": email, "password": PW}).json()[
            "token"
        ]
    }


@pytest.fixture()
def admin(c):
    mk_user("admin@x.io", "admin")
    return login(c, "admin@x.io")


@pytest.fixture()
def fake_platform(monkeypatch):
    """The platforms' answers: which boards exist, and how often each was
    fetched."""
    state = {
        "boards": {
            ("greenhouse", "stripe"): 3,
            ("lever", "spotify"): 1,
            ("ashby", "Notion"): 2,
            ("lever", "eu/acme"): 1,
        },
        "calls": [],
    }

    def fetch(vendor, board):
        state["calls"].append((vendor, board))
        n = state["boards"].get((vendor, board))
        if n is None:
            raise BoardGone(board)
        return [
            fj(f"Role {i}", board=board, vendor=vendor, company=board.title())
            for i in range(n)
        ]

    monkeypatch.setattr(sync, "default_fetcher", fetch)
    return state


# ── slug rules ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "vendor,typed,key",
    [
        ("greenhouse", " Stripe/ ", "stripe"),
        ("ashby", "Notion", "Notion"),
        ("smartrecruiters", "BoschGroup", "BoschGroup"),
        ("lever", "EU/Acme", "eu/acme"),
        ("personio", "Acme.DE", "acme.de"),
        ("recruitee", "Bunq", "bunq"),
        (
            "workday",
            "nvidia.wd5/NVIDIAExternalCareerSite",
            "nvidia.wd5/NVIDIAExternalCareerSite",
        ),
    ],
)
def test_typed_slugs_become_the_canonical_key(vendor, typed, key):
    assert VENDORS[vendor].normalize_board(typed) == key


@pytest.mark.parametrize(
    "vendor,typed",
    [
        ("greenhouse", "has space"),
        ("greenhouse", ""),
        ("greenhouse", "embed"),
        ("greenhouse", "a/b"),
        ("personio", "acme"),
        ("personio", "acme.fr"),
        ("workday", "nvidia"),
        ("workday", "nvidia.wd5/wday"),
        ("lever", "eu/"),
    ],
)
def test_invalid_slugs_are_refused_with_a_reason(vendor, typed):
    with db.session() as s, pytest.raises(ServiceError):
        boards.add_board(s, vendor, typed)


def test_slug_typed_by_hand_matches_what_discovery_stored():
    """'Stripe' typed by an admin and 'stripe' found in Common Crawl must be
    one board, not two."""
    with db.session() as s:
        boards.upsert_discovered(s, "greenhouse", {"stripe": 40})
    with db.session() as s:
        b = boards.add_board(s, "greenhouse", "Stripe", added_by=7)
        assert (b.board, b.source, b.status, b.hits, b.added_by) == (
            "stripe",
            "manual",
            "enabled",
            40,
            7,
        )  # adopted
    with db.session() as s:
        assert len(list(s.scalars(select(Board)))) == 1


def test_every_vendor_has_help_text_for_the_admin_form():
    for v in VENDORS.values():
        assert v.label and v.example and v.hint, v.name
        assert (
            v.normalize_board(v.example) == v.example.lower()
            if not v.case_sensitive and v.name != "workday"
            else True
        )
        assert (
            v.public_url(v.normalize_board(v.example)).startswith("https://")
            or v.name == "workday"
        )


def test_parse_entry_formats():
    assert boards.parse_entry("greenhouse:stripe") == ("greenhouse", "stripe")
    assert boards.parse_entry("Greenhouse Stripe") == ("greenhouse", "stripe")
    assert boards.parse_entry(
        "https://boards.greenhouse.io/Stripe/jobs/9"
    ) == ("greenhouse", "stripe")
    assert boards.parse_entry("jobs.ashbyhq.com/Notion") == ("ashby", "Notion")
    assert boards.parse_entry("lever:eu/acme") == ("lever", "eu/acme")
    for bad in ("stripe", "nope:stripe", "https://example.com/x"):
        with pytest.raises(ServiceError):
            boards.parse_entry(bad)


# ── probe ───────────────────────────────────────────────────────────────
def test_probe_outcomes(fake_platform):
    ok = boards.probe("greenhouse", "stripe")
    assert (
        ok["exists"]
        and ok["job_count"] == 3
        and ok["sample"][:2] == ["Role 0", "Role 1"]
        and ok["company"] == "Stripe"
    )
    assert ok["public_url"] == "https://boards.greenhouse.io/stripe"
    gone = boards.probe("greenhouse", "nope")
    assert not gone["exists"] and "not found on Greenhouse" in gone["error"]

    def http404(v, b):
        r = requests.Response()
        r.status_code = 404
        raise requests.HTTPError(response=r)

    def http503(v, b):
        r = requests.Response()
        r.status_code = 503
        raise requests.HTTPError(response=r)

    def boom(v, b):
        raise TimeoutError("slow")

    assert "not found" in boards.probe("lever", "x", http404)["error"]
    assert "HTTP 503" in boards.probe("lever", "x", http503)["error"]
    assert (
        "could not check" in boards.probe("lever", "x", boom)["error"]
    )  # a failed check never raises


# ── API: admin only ─────────────────────────────────────────────────────
ADMIN_ENDPOINTS = [
    ("get", "/admin/vendors", None),
    ("get", "/admin/boards/probe?vendor=greenhouse&slug=stripe", None),
    ("post", "/admin/boards", {"vendor": "greenhouse", "slug": "stripe"}),
    ("post", "/admin/boards/import", {"entries": ["greenhouse:stripe"]}),
    ("get", "/admin/inventory", None),
    ("get", "/admin/boards?source=manual", None),
]


def test_only_admins_can_add_or_inspect_boards(c, fake_platform):
    mk_user("admin@x.io", "admin")
    mk_user("u@x.io")
    user = login(c, "u@x.io")
    for method, path, body in ADMIN_ENDPOINTS:
        call = getattr(c, method)
        kw = {"json": body} if body is not None else {}
        assert call(path, **kw).status_code == 401, path  # anonymous
        assert call(path, headers=user, **kw).status_code == 403, (
            path
        )  # a normal user
    assert (
        fake_platform["calls"] == []
    )  # nothing was fetched on behalf of a non-admin
    with db.session() as s:
        assert s.scalar(select(Board)) is None
    admin = login(c, "admin@x.io")
    for method, path, body in ADMIN_ENDPOINTS:
        kw = {"json": body} if body is not None else {}
        assert getattr(c, method)(path, headers=admin, **kw).status_code in (
            200,
            201,
        ), path


def test_an_mcp_token_cannot_add_boards(c, fake_platform):
    """Agent tokens belong to users, who are never admins by way of MCP; they
    don't even work on the REST API."""
    mk_user("admin@x.io", "admin")
    h = login(c, "admin@x.io")
    mcp = c.post("/me/mcp-tokens", headers=h, json={}).json()["token"]
    r = c.post(
        "/admin/boards",
        headers={"Authorization": f"Bearer {mcp}"},
        json={"vendor": "greenhouse", "slug": "stripe"},
    )
    assert r.status_code == 401
    from livejobs import mcp_server

    assert not any(
        "board" in t
        for t in [
            t.__name__ for t in (mcp_server.save_job, mcp_server.search_jobs)
        ]
    )


# ── API: adding ─────────────────────────────────────────────────────────
def test_add_by_slug_checks_the_platform_and_stores_jobs_immediately(
    c, admin, fake_platform
):
    r = c.post(
        "/admin/boards",
        headers=admin,
        json={"vendor": "greenhouse", "slug": "Stripe"},
    )
    assert r.status_code == 201
    b = r.json()
    assert (b["board"], b["source"], b["status"], b["job_count"]) == (
        "stripe",
        "manual",
        "enabled",
        3,
    )
    assert (
        b["added_by"] is not None
        and b["public_url"] == "https://boards.greenhouse.io/stripe"
        and b["company_name"] == "Stripe"
    )
    assert fake_platform["calls"] == [
        ("greenhouse", "stripe")
    ]  # exactly one fetch: the check *is* the first sync
    assert c.get("/public/stats").json()["open_jobs"] == 3
    with db.session() as s:
        assert (
            s.get(Board, b["id"]).next_sync_at is not None
        )  # scheduled from now on


def test_a_typo_is_refused_and_nothing_is_stored(c, admin, fake_platform):
    r = c.post(
        "/admin/boards",
        headers=admin,
        json={"vendor": "greenhouse", "slug": "strpe"},
    )
    assert (
        r.status_code == 422
        and "not found on Greenhouse" in r.json()["detail"]
    )
    with db.session() as s:
        assert s.scalar(select(Board)) is None
    # an admin who knows better can skip the check
    r = c.post(
        "/admin/boards",
        headers=admin,
        json={"vendor": "greenhouse", "slug": "newcorp", "verify": False},
    )
    assert (
        r.status_code == 201
        and r.json()["job_count"] == 0
        and r.json()["source"] == "manual"
    )


def test_add_by_url_and_validation_errors(c, admin, fake_platform):
    r = c.post(
        "/admin/boards",
        headers=admin,
        json={"url": "https://jobs.lever.co/spotify/abc-123"},
    )
    assert r.status_code == 201 and (
        r.json()["vendor"],
        r.json()["board"],
    ) == ("lever", "spotify")
    r = c.post(
        "/admin/boards",
        headers=admin,
        json={"url": "https://jobs.eu.lever.co/acme"},
    )
    assert r.status_code == 201 and r.json()["board"] == "eu/acme"
    assert (
        c.post(
            "/admin/boards",
            headers=admin,
            json={"url": "https://example.com/careers"},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/admin/boards",
            headers=admin,
            json={"vendor": "ashby", "url": "https://jobs.lever.co/spotify"},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/admin/boards",
            headers=admin,
            json={"vendor": "nope", "slug": "x"},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/admin/boards",
            headers=admin,
            json={"vendor": "greenhouse", "slug": "bad slug"},
        ).status_code
        == 400
    )
    assert c.post("/admin/boards", headers=admin, json={}).status_code == 400


def test_adding_twice_is_harmless_and_can_set_an_interval(
    c, admin, fake_platform
):
    a = c.post(
        "/admin/boards",
        headers=admin,
        json={"vendor": "ashby", "slug": "notion", "sync_interval_min": 30},
    )
    assert (
        a.status_code == 422
    )  # Ashby slugs keep their casing: 'notion' isn't 'Notion'
    a = c.post(
        "/admin/boards",
        headers=admin,
        json={"vendor": "ashby", "slug": "Notion", "sync_interval_min": 30},
    ).json()
    b = c.post(
        "/admin/boards",
        headers=admin,
        json={"vendor": "ashby", "slug": "Notion"},
    ).json()
    assert a["id"] == b["id"] and a["sync_interval_min"] == 30
    assert len(c.get("/admin/boards", headers=admin).json()) == 1
    assert (
        c.post(
            "/admin/boards",
            headers=admin,
            json={"vendor": "ashby", "slug": "Notion", "sync_interval_min": 1},
        ).status_code
        == 400
    )


def test_probe_endpoint_reports_without_adding(c, admin, fake_platform):
    r = c.get(
        "/admin/boards/probe?vendor=greenhouse&slug=stripe", headers=admin
    ).json()
    assert (
        r["exists"]
        and r["job_count"] == 3
        and r["already"] is None
        and "jobs" not in r
    )
    with db.session() as s:
        assert s.scalar(select(Board)) is None
    c.post(
        "/admin/boards",
        headers=admin,
        json={"vendor": "greenhouse", "slug": "stripe"},
    )
    again = c.get(
        "/admin/boards/probe?url=https://boards.greenhouse.io/stripe",
        headers=admin,
    ).json()
    assert (
        again["already"]["source"] == "manual"
        and again["already"]["status"] == "enabled"
    )
    assert (
        c.get(
            "/admin/boards/probe?vendor=greenhouse&slug=zzz", headers=admin
        ).json()["exists"]
        is False
    )


def test_bulk_import_reports_each_line(c, admin, fake_platform):
    entries = [
        "# my list",
        "greenhouse:stripe",
        "https://jobs.lever.co/spotify",
        "greenhouse:stripe",
        "ashby:Notion",
        "greenhouse:doesnotexist",
        "stripe",
        "lever:eu/acme",
        "",
    ]
    r = c.post(
        "/admin/boards/import", headers=admin, json={"entries": entries}
    ).json()
    by_input = {
        x["input"]: x for x in reversed(r["results"])
    }  # reversed: the first occurrence of a repeated line wins
    assert (
        by_input["greenhouse:stripe"]["status"] == "added"
        and by_input["greenhouse:stripe"]["job_count"] == 3
    )
    assert [
        x["status"] for x in r["results"] if x["input"] == "greenhouse:stripe"
    ] == ["added", "duplicate_in_list"]
    assert by_input["https://jobs.lever.co/spotify"]["status"] == "added"
    assert by_input["greenhouse:doesnotexist"]["status"] == "not_found"
    assert (
        by_input["stripe"]["status"] == "invalid"
        and "platform:slug" in by_input["stripe"]["error"]
    )
    assert r["summary"] == {
        "added": 4,
        "duplicate_in_list": 1,
        "not_found": 1,
        "invalid": 1,
    }
    assert (
        len(c.get("/admin/boards?source=manual", headers=admin).json()) == 4
    )  # the good lines were added anyway
    again = c.post(
        "/admin/boards/import",
        headers=admin,
        json={"entries": ["greenhouse:stripe"]},
    ).json()
    assert again["summary"] == {"exists": 1}


def test_source_filter_and_vendor_catalog(c, admin, fake_platform):
    with db.session() as s:
        boards.upsert_discovered(s, "lever", {"found-by-crawl": 5})
    c.post(
        "/admin/boards",
        headers=admin,
        json={"vendor": "greenhouse", "slug": "stripe"},
    )
    assert [
        b["board"]
        for b in c.get("/admin/boards?source=manual", headers=admin).json()
    ] == ["stripe"]
    assert [
        b["board"]
        for b in c.get("/admin/boards?source=crawl", headers=admin).json()
    ] == ["found-by-crawl"]
    cat = c.get("/admin/vendors", headers=admin).json()
    assert {v["vendor"] for v in cat} == set(VENDORS) and all(
        v["example"] and v["hint"] for v in cat
    )


def test_cli_add_and_import(capsys, monkeypatch, fake_platform, tmp_path):
    monkeypatch.setattr(db, "init", lambda url=None: db.engine())
    cli.main(["boards", "add", "greenhouse", "Stripe", "--interval", "30"])
    out = capsys.readouterr().out
    assert "added" in out and "3 jobs" in out
    with db.session() as s:
        b = s.scalar(select(Board))
        assert (b.board, b.source, b.sync_interval_min) == (
            "stripe",
            "manual",
            30,
        )
    with pytest.raises(SystemExit) as e:
        cli.main(["boards", "add", "greenhouse", "nope"])
    assert e.value.code == 1 and "not_found" in capsys.readouterr().out
    f = tmp_path / "boards.txt"
    f.write_text("lever:spotify\nhttps://jobs.ashbyhq.com/Notion\n")
    cli.main(["boards", "import", str(f)])
    cli.main(
        [
            "boards",
            "add-url",
            "https://boards.greenhouse.io/stripe",
            "--no-verify",
        ]
    )
    with db.session() as s:
        assert len(list(s.scalars(select(Board)))) == 3


# ── monthly Common Crawl refresh ────────────────────────────────────────
class Crawl:
    """A stand-in for index.commoncrawl.org: two pages per query, each listing
    a few boards."""

    def __init__(self, fail_pages=()):
        self.fail_pages, self.fetched = set(fail_pages), []

    def pages(self, crawl, url, match):
        return 2

    def fetch(self, crawl, url, match, page):
        self.fetched.append((crawl, url, page))
        if (url, page) in self.fail_pages:
            raise RuntimeError("504 Gateway Time-out")
        host = url
        return [
            f"https://{host}/co{page}a/jobs/1",
            f"https://{host}/co{page}a/jobs/2",
            f"https://{host}/co{page}b",
        ]


def refresh(crawl_id, fake, vendors=("greenhouse",)):
    return inventory.refresh(
        list(vendors),
        1,
        log=lambda m: None,
        crawls_fn=lambda n: [crawl_id],
        fetch=fake.fetch,
        pages_of=fake.pages,
    )


def test_first_refresh_ingests_the_crawl_and_records_it():
    fake = Crawl()
    status, run = refresh("CC-MAIN-2026-43", fake)
    assert (
        status == "complete"
        and run.crawl == "CC-MAIN-2026-43"
        and run.pages_failed == 0
    )
    with db.session() as s:
        found = {
            (b.board, b.status, b.source) for b in s.scalars(select(Board))
        }
        assert found == {
            (n, "enabled", "crawl") for n in ("co0a", "co0b", "co1a", "co1b")
        }
        r = s.scalar(select(InventoryRun))
        assert (
            r.status == "complete"
            and r.boards_new == 4
            and r.pages_fetched == 6
            and r.finished_at
        )  # 3 queries x 2 pages
    assert (
        len(sync.claim_due(10)) == 4
    )  # found boards are synced automatically


def test_refresh_is_idempotent_so_it_can_be_scheduled_daily():
    fake = Crawl()
    refresh("CC-MAIN-2026-43", fake)
    with db.session() as s:
        before = {b.board: b.hits for b in s.scalars(select(Board))}
    n = len(fake.fetched)
    status, run = refresh("CC-MAIN-2026-43", fake)
    assert (status, run) == ("up_to_date", None) and len(
        fake.fetched
    ) == n  # nothing fetched, nothing recorded
    with db.session() as s:
        assert len(list(s.scalars(select(InventoryRun)))) == 1
        assert {
            b.board: b.hits for b in s.scalars(select(Board))
        } == before  # not double-counted


def test_a_new_monthly_crawl_runs_again_and_adds_only_new_boards():
    _, first = refresh("CC-MAIN-2026-39", Crawl())
    with db.session() as s:
        before = {b.board: b.hits for b in s.scalars(select(Board))}
    status, run = refresh("CC-MAIN-2026-43", Crawl())
    assert status == "complete" and first.boards_new == 4
    with db.session() as s:
        assert len(list(s.scalars(select(InventoryRun)))) == 2
        assert (
            run.boards_new == 0
        )  # the same four boards: nothing new to report
        assert all(
            b.hits == 2 * before[b.board] for b in s.scalars(select(Board))
        )  # seen again in the new crawl


def test_a_partial_run_resumes_only_what_is_missing():
    flaky = Crawl(fail_pages={("boards.greenhouse.io", 1)})
    status, run = refresh("CC-MAIN-2026-43", flaky)
    assert status == "partial" and run.pages_failed == 1
    healthy = Crawl()
    status, run = refresh("CC-MAIN-2026-43", healthy)
    assert status == "complete"
    assert [(u, p) for _, u, p in healthy.fetched] == [
        ("boards.greenhouse.io", 1)
    ]  # only the page that failed
    with db.session() as s:
        assert [
            r.status
            for r in s.scalars(select(InventoryRun).order_by(InventoryRun.id))
        ] == ["partial", "complete"]
    assert refresh("CC-MAIN-2026-43", healthy)[0] == "up_to_date"


def test_manual_boards_are_untouched_by_a_refresh():
    with db.session() as s:
        b = boards.add_board(s, "greenhouse", "co0a", enable=True, added_by=3)
        boards.update_board(s, b.id, sync_interval_min=15)
        bid = b.id
    refresh("CC-MAIN-2026-43", Crawl())
    with db.session() as s:
        b = s.get(Board, bid)
        assert (b.source, b.status, b.sync_interval_min, b.added_by) == (
            "manual",
            "enabled",
            15,
            3,
        )  # discovery only adds hits
        assert b.hits > 0


def test_refresh_survives_index_failures_and_records_them():
    def down(n):
        raise requests.ConnectionError("index.commoncrawl.org unreachable")

    status, run = inventory.refresh(
        ["greenhouse"], 1, log=lambda m: None, crawls_fn=down
    )
    assert status == "failed" and "unreachable" in run.error

    class NoPages(Crawl):
        def pages(self, *a):
            raise requests.HTTPError("504")

    status, run = refresh("CC-MAIN-2026-43", NoPages())
    assert (
        status == "partial" and run.pages_failed == 3
    )  # three queries couldn't even list their pages


def test_cli_refresh_exit_codes(monkeypatch):
    monkeypatch.setattr(db, "init", lambda url=None: db.engine())
    for status, code in [
        ("complete", 0),
        ("up_to_date", 0),
        ("already_running", 0),
        ("partial", 75),
        ("failed", 1),
    ]:
        monkeypatch.setattr(
            inventory,
            "refresh",
            lambda v, w, status=status, **kw: (status, None),
        )
        with pytest.raises(SystemExit) as e:
            cli.main(["inventory", "--refresh"])
        assert (
            e.value.code == code
        )  # 75 tells systemd/cron "incomplete, try again later"


# ── schema upgrade & metrics ────────────────────────────────────────────
def test_upgrade_marks_pre_existing_hand_added_boards_manual():
    with db.engine().begin() as c:
        c.execute(text("ALTER TABLE boards DROP COLUMN source"))
        c.execute(text("ALTER TABLE boards DROP COLUMN added_by"))
        c.execute(
            text(
                "INSERT INTO boards (vendor, board, status, company_name, "
                "hits, first_seen, last_seen, consecutive_failures, "
                "consecutive_gone, job_count) "
                "VALUES "
                "('greenhouse','handadded','enabled','',0,'2025-01-01',"
                "'2025-01-01',0,0,0), "
                "       "
                "('greenhouse','crawled','discovered','',9,'2025-01-01','2025-01-01',0,0,0)"
            )
        )
    assert any("boards.source" in x for x in migrations.ensure_schema())
    with db.session() as s:
        assert {b.board: b.source for b in s.scalars(select(Board))} == {
            "handadded": "manual",
            "crawled": "crawl",
        }
    assert migrations.ensure_schema() == []


def test_metrics_expose_inventory_and_board_sources(fake_platform):
    c = TestClient(
        __import__("livejobs.site", fromlist=["app"]).app,
        client=("127.0.0.1", 1),
    )
    assert (
        "livejobs_inventory_last_success_timestamp_seconds 0.0"
        in c.get("/metrics").text
    )  # never run
    refresh("CC-MAIN-2026-43", Crawl())
    with db.session() as s:
        boards.add_board(s, "greenhouse", "mine", enable=True)
    metrics.collector.reset()
    t = c.get("/metrics").text
    assert (
        'livejobs_boards_by_source{source="manual",status="enabled"} 1.0' in t
    )
    assert (
        'livejobs_boards_by_source{source="crawl",status="enabled"} 4.0' in t
    )
    assert 'livejobs_inventory_runs_total{status="complete"} 1.0' in t
    assert "livejobs_inventory_boards_new_total 4.0" in t
    ts = float(
        next(
            line
            for line in t.splitlines()
            if line.startswith(
                "livejobs_inventory_last_success_timestamp_seconds "
            )
        ).split()[1]
    )
    assert ts > 1.7e9


def test_import_has_no_200_line_limit_and_syncs_every_board_in_parallel():
    import threading

    names = [f"co{i}" for i in range(450)]
    threads, lock = set(), threading.Lock()

    def fetcher(vendor, slug):
        with lock:
            threads.add(threading.current_thread().name)
        return [fj("Engineer", board=slug, vendor=vendor, company=slug)]

    with db.session() as s:
        res = boards.add_many(
            s, [f"lever:{n}" for n in names], fetcher=fetcher, workers=8
        )
    assert [r["status"] for r in res].count("added") == 450
    with db.session() as s:
        assert (
            s.scalar(select(func.count()).select_from(Job)) == 450
        )  # stored from the check; no second fetch
        assert (
            s.scalar(
                select(func.count())
                .select_from(Board)
                .where(Board.last_ok_at.is_not(None))
            )
            == 450
        )
    assert len(threads) > 1  # the checks ran on a pool, not one thread


def _wait_import(c, admin, job_id):
    import time

    for _ in range(300):
        j = c.get(f"/admin/boards/import/{job_id}", headers=admin).json()
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("import did not finish")


def test_a_long_import_runs_in_the_background_with_progress(
    c, admin, monkeypatch
):
    monkeypatch.setattr(
        sync,
        "default_fetcher",
        lambda v, b: (
            [fj("Engineer", board=b, vendor=v)]
            if b != "b3"
            else (_ for _ in ()).throw(BoardGone(b))
        ),
    )
    lines = [f"lever:b{i}" for i in range(250)] + ["nonsense line"]
    r = c.post("/admin/boards/import", json={"entries": lines}, headers=admin)
    assert (
        r.status_code == 202 and r.json()["total"] == 251
    )  # answered at once; no 200-line cap
    j = _wait_import(c, admin, r.json()["id"])
    assert j["state"] == "done" and j["done"] == 251
    assert j["summary"] == {"added": 249, "not_found": 1, "invalid": 1}
    assert {p["status"] for p in j["problems"]} == {
        "not_found",
        "invalid",
    }  # only lines needing attention are kept
    assert (
        c.get("/admin/boards/import/current", headers=admin).json()["id"]
        == j["id"]
    )
    assert c.get("/admin/boards/counts", headers=admin).json()["manual"] == 249


def test_short_imports_still_answer_inline(c, admin, fake_platform):
    r = c.post(
        "/admin/boards/import",
        json={"entries": ["greenhouse:stripe"]},
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["summary"] == {"added": 1}


def test_only_one_background_import_at_a_time(c, admin, monkeypatch):
    import threading

    gate = threading.Event()
    monkeypatch.setattr(
        sync, "default_fetcher", lambda v, b: gate.wait(5) and []
    )
    lines = [f"lever:c{i}" for i in range(30)]
    first = c.post(
        "/admin/boards/import", json={"entries": lines}, headers=admin
    )
    assert first.status_code == 202
    assert (
        c.post(
            "/admin/boards/import", json={"entries": lines}, headers=admin
        ).status_code
        == 409
    )
    gate.set()
    assert _wait_import(c, admin, first.json()["id"])["state"] == "done"


def test_import_endpoints_are_admin_only(c):
    mk_user("u@x.io", "user")
    h = login(c, "u@x.io")
    assert c.get("/admin/boards/import/current", headers=h).status_code == 403
    assert (
        c.post(
            "/admin/boards/import",
            json={"entries": ["lever:x"] * 30},
            headers=h,
        ).status_code
        == 403
    )


def test_add_many_works_in_chunks_so_memory_stays_flat(monkeypatch):
    sizes = []
    real = sync.sync_many
    monkeypatch.setattr(
        sync,
        "sync_many",
        lambda ids, *a, **k: (sizes.append(len(ids)), real(ids, *a, **k))[1],
    )
    with db.session() as s:
        res = boards.add_many(
            s,
            [f"lever:m{i}" for i in range(100)],
            fetcher=lambda v, b: [fj("E", board=b, vendor=v)],
            workers=8,
        )
    assert (
        len(res) == 100 and sizes and max(sizes) <= 8 and len(sizes) >= 12
    )  # 8 boards' jobs held at a time, not 100


def test_boards_list_sorts_by_name_status_jobs_and_last_synced(c, admin):
    from datetime import timedelta
    from livejobs.orm import utcnow

    now = utcnow()
    with db.session() as s:
        for slug, status, jobs, synced in [
            ("bravo", "enabled", 5, now - timedelta(hours=3)),
            ("alpha", "disabled", 50, None),
            ("charlie", "enabled", 20, now - timedelta(hours=1)),
        ]:
            s.add(
                Board(
                    vendor="lever",
                    board=slug,
                    status=status,
                    job_count=jobs,
                    last_sync_at=synced,
                )
            )

    def order(sort, d="asc"):
        return [
            b["board"]
            for b in c.get(
                f"/admin/boards?sort={sort}&dir={d}", headers=admin
            ).json()
        ]

    assert order("board") == ["alpha", "bravo", "charlie"] and order(
        "board", "desc"
    ) == ["charlie", "bravo", "alpha"]
    assert order("jobs", "desc") == ["alpha", "charlie", "bravo"] and order(
        "jobs"
    ) == ["bravo", "charlie", "alpha"]
    assert order("status") == ["alpha", "bravo", "charlie"] and order(
        "status", "desc"
    ) == ["bravo", "charlie", "alpha"]  # disabled < enabled
    assert order("synced", "desc") == [
        "charlie",
        "bravo",
        "alpha",
    ]  # never synced goes last...
    assert order("synced") == [
        "bravo",
        "charlie",
        "alpha",
    ]  # ...in either direction
    assert (
        c.get("/admin/boards?sort=password", headers=admin).status_code == 400
    )
    assert "last_sync_at" in c.get("/admin/boards", headers=admin).json()[0]


def test_sync_all_refreshes_every_enabled_board_in_the_background(
    c, admin, monkeypatch
):
    import time

    calls = []
    monkeypatch.setattr(
        sync,
        "default_fetcher",
        lambda v, b: calls.append(b) or [fj("Role", board=b, vendor=v)],
    )
    with db.session() as s:
        for i in range(30):
            boards.add_board(s, "lever", f"s{i}", enable=True)
        boards.add_board(s, "lever", "off", enable=False)
    assert (
        sync.claim_due(100) and True
    )  # everything is leased now, so a scheduled run would find nothing due
    r = c.post("/admin/boards/sync-all", headers=admin)
    assert r.status_code == 202 and r.json()["total"] == 30
    for _ in range(200):
        st = c.get("/admin/boards/sync-all", headers=admin).json()
        if st["state"] != "running":
            break
        time.sleep(0.1)
    assert (
        st["state"] == "done"
        and st["done"] == 30
        and st["ok"] == 30
        and st["jobs"] == 30
    )
    assert sorted(calls) == sorted(
        f"s{i}" for i in range(30)
    )  # forced past the schedule; the disabled one is skipped
    assert (
        c.post("/admin/boards/sync-all", headers=admin).status_code == 202
    )  # can run again once finished


def test_sync_all_is_admin_only_and_one_at_a_time(c, admin, monkeypatch):
    import threading

    mk_user("u@x.io", "user")
    assert (
        c.post(
            "/admin/boards/sync-all", headers=login(c, "u@x.io")
        ).status_code
        == 403
    )
    gate = threading.Event()
    monkeypatch.setattr(
        sync, "default_fetcher", lambda v, b: gate.wait(5) and []
    )
    mk_board("lever", "one")
    assert c.post("/admin/boards/sync-all", headers=admin).status_code == 202
    assert c.post("/admin/boards/sync-all", headers=admin).status_code == 409
    gate.set()
    import time

    for _ in range(100):
        if (
            c.get("/admin/boards/sync-all", headers=admin).json()["state"]
            != "running"
        ):
            break
        time.sleep(0.1)
