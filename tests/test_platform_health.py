"""Per-platform history, the drop alert, the live canary, and the staffing
and label filters that come with them."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from livejobs import canary, db, health, migrations, site
from livejobs.api import app
from livejobs.orm import Board, CanaryResult, Job, VendorSnapshot, utcnow
from livejobs.services import boards, matching, prefs, search, sync

from .helpers import fj, mk_board, mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")
PW = "correct-horse-battery"


def login(c, email):
    r = c.post("/auth/login", json={"email": email, "password": PW})
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def c():
    return TestClient(app)


@pytest.fixture()
def admin(c):
    mk_user("admin@x.io", "admin")
    return login(c, "admin@x.io")


# ── the judgement ───────────────────────────────────────────────────────
def test_judge_needs_history_and_flags_big_drops_only():
    flat = [1000, 1010, 990, 1005, 995, 1000, 1002]
    assert health.judge([1000, 1001], 10)["state"] == "learning"
    assert health.judge(flat, 1003)["state"] == "ok"
    assert health.judge(flat, 150)["state"] == "drop"
    assert health.judge(flat, 5000)["state"] == "spike"
    # a perfectly flat history must not flag a swing of two postings
    assert health.judge([50] * 7, 48)["state"] == "ok"
    # ...but does flag losing most of them
    assert health.judge([50] * 7, 10)["state"] == "drop"
    # noisy history tolerates a normal dip
    assert (
        health.judge([800, 1200, 900, 1100, 1000, 1150, 850], 780)["state"]
        == "ok"
    )


def _snap(vendor, jobs, days_ago, now):
    day = (now - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    with db.session() as s:
        s.merge(
            VendorSnapshot(
                day=day,
                vendor=vendor,
                active_jobs=jobs,
                boards_enabled=3,
                boards_failing=0,
                taken_at=now,
            )
        )


def test_report_flags_a_platform_that_lost_its_jobs():
    now = utcnow()
    for i, n in zip(range(7, 0, -1), [900, 920, 880, 910, 905, 915, 900]):
        _snap("lever", n, i, now)
        _snap("ashby", n, i, now)
    _snap("lever", 40, 0, now)
    _snap("ashby", 905, 0, now)
    rows = {r["vendor"]: r for r in health.report(now)}
    assert rows["lever"]["state"] == "drop" and rows["lever"]["change"] < -0.9
    assert rows["ashby"]["state"] == "ok"
    assert (
        len(rows["lever"]["series"]) == 8 and rows["lever"]["canary"] is None
    )


def test_snapshot_records_today_once_and_updates_it():
    b = mk_board("lever", "a")
    sync.sync_board(b, lambda v, s: [fj("T1", board=s, vendor=v)])
    health.snapshot_vendors()
    sync.sync_board(
        b,
        lambda v, s: [
            fj("T1", board=s, vendor=v),
            fj("T2", board=s, vendor=v),
        ],
    )
    health.snapshot_vendors()  # the same day again: overwritten, not added
    with db.session() as s:
        rows = list(s.scalars(select(VendorSnapshot)))
    assert [(r.vendor, r.active_jobs, r.boards_enabled) for r in rows] == [
        ("lever", 2, 1)
    ]


def test_anomaly_shows_in_metrics_and_the_admin_endpoint(c, admin):
    from livejobs import metrics

    now = utcnow()
    for i in range(7, 0, -1):
        _snap("lever", 500 + i, i, now)
    _snap("lever", 20, 0, now)
    metrics.collector.reset()
    text = TestClient(site.app, client=("127.0.0.1", 1)).get("/metrics").text
    assert (
        'livejobs_vendor_anomaly{direction="drop",vendor="lever"} 1.0' in text
    )
    assert (
        'livejobs_vendor_anomaly{direction="spike",vendor="lever"} 0.0' in text
    )
    rows = c.get("/admin/health", headers=admin).json()
    assert rows[0]["vendor"] == "lever" and rows[0]["state"] == "drop"
    mk_user("u@x.io")
    assert (
        c.get("/admin/health", headers=login(c, "u@x.io")).status_code == 403
    )


# ── the canary ──────────────────────────────────────────────────────────
def test_canary_passes_on_real_postings_and_fails_on_empty_or_broken_ones():
    mk_board("lever", "good")
    b = mk_board("ashby", "empty")
    with db.session() as s:
        s.get(Board, b).job_count = 5
        s.get(Board, b).last_ok_at = utcnow()

    def fetcher(vendor, board):
        if board == "good":
            return [fj("Role", board=board, vendor=vendor)]
        if board == "empty":
            return []
        raise RuntimeError("markup changed")

    assert canary.judge([])[0] is False
    assert canary.judge([fj("", url="x")])[0] is False  # no title / no link
    res = canary.check("ashby", fetcher)
    # the DB board is empty; the fixed samples then fail with the exception
    assert res["ok"] is False and res["error"]
    with db.session() as s:
        s.get(Board, mk_board("lever", "good2")).job_count = 3
    out = canary.run(
        ["lever"],
        lambda v, b: [fj("Role", board=b, vendor=v, url="https://x/1")],
    )
    assert out and out[0]["ok"] is True
    with db.session() as s:
        row = s.get(CanaryResult, "lever")
        assert row.ok and row.jobs == 1


def test_canary_prefers_small_enabled_boards_from_the_database_then_samples():
    with db.session() as s:
        for slug, n in (("big", 900), ("small", 3), ("mid", 40)):
            b = boards.add_board(s, "lever", slug, enable=True)
            b.job_count, b.last_ok_at = n, utcnow()
        failing = boards.add_board(s, "lever", "broken", enable=True)
        failing.job_count, failing.last_ok_at, failing.consecutive_failures = (
            5,
            utcnow(),
            2,
        )
    got = canary.candidates("lever")
    assert got[:3] == ["small", "mid", "big"] and "broken" not in got
    assert "spotify" in got  # the fixed sample is the fallback


def test_canary_env_boards_and_nothing_to_check(monkeypatch):
    monkeypatch.setattr(
        "livejobs.config.CANARY_BOARDS", "breezy:mine, lever:other"
    )
    assert canary.candidates("breezy")[0] == "mine"
    assert canary.check("bamboohr", lambda v, b: []) is None  # no sample known


def test_canary_endpoint_runs_in_the_background(c, admin, monkeypatch):
    import time

    monkeypatch.setattr(canary, "SAMPLES", {"lever": ["sample"]})
    monkeypatch.setattr(
        sync,
        "default_fetcher",
        lambda v, b: [fj("R", board=b, vendor=v, url="https://x/r")],
    )
    assert c.post("/admin/health/canary", headers=admin).status_code == 202
    for _ in range(100):
        rows = c.get("/admin/health", headers=admin).json()
        if rows and rows[0]["canary"]:
            break
        time.sleep(0.1)
    assert rows[0]["canary"]["ok"] is True


# ── labels on postings ──────────────────────────────────────────────────
def _stored():
    b = mk_board("lever", "acme")
    sync.sync_board(
        b,
        lambda v, s: [
            fj(
                "Senior Backend Engineer", board=s, vendor=v, loc="Remote - US"
            ),
            fj(
                "Software Engineering Intern",
                board=s,
                vendor=v,
                loc="Berlin, Germany",
            ),
            fj("Director of Sales", board=s, vendor=v, loc="London (Hybrid)"),
            fj("Support Specialist", board=s, vendor=v, loc="Toronto, ON"),
        ],
    )
    return b


def test_sync_labels_every_posting():
    _stored()
    with db.session() as s:
        rows = {j.title: j for j in s.scalars(select(Job))}
    j = rows["Senior Backend Engineer"]
    assert (j.workplace, j.seniority, j.country) == ("remote", "senior", "US")
    j = rows["Software Engineering Intern"]
    assert (j.workplace, j.seniority, j.country) == ("", "intern", "DE")
    j = rows["Director of Sales"]
    assert (j.workplace, j.seniority, j.country) == (
        "hybrid",
        "executive",
        "GB",
    )
    assert rows["Support Specialist"].country == "CA"


def test_search_filters_on_the_labels():
    _stored()
    with db.session() as s:

        def titles(**kw):
            r = search.search_jobs(s, None, browse=True, **kw)
            return sorted(h.job.title for h in r["hits"])

        assert titles(workplace="remote") == ["Senior Backend Engineer"]
        assert titles(remote_only=True) == ["Senior Backend Engineer"]
        assert titles(workplace="hybrid") == ["Director of Sales"]
        assert titles(seniority="intern,executive") == [
            "Director of Sales",
            "Software Engineering Intern",
        ]
        assert titles(seniority=["senior"]) == ["Senior Backend Engineer"]
        assert titles(country="ca,de") == [
            "Software Engineering Intern",
            "Support Specialist",
        ]
        assert len(titles(seniority="mid")) == 1
        with pytest.raises(Exception, match="seniority"):
            titles(seniority="wizard")
        with pytest.raises(Exception, match="two-letter"):
            titles(country="Germany")


def test_agency_boards_are_flagged_and_can_be_hidden_and_overridden(c, admin):
    a = mk_board("lever", "acme-staffing")
    ok = mk_board("lever", "stripe")
    for bid in (a, ok):
        sync.sync_board(
            bid,
            lambda v, s: [
                fj("Engineer", board=s, vendor=v, url=f"https://x/{s}")
            ],
        )
    with db.session() as s:
        assert s.get(Board, a).is_agency and not s.get(Board, ok).is_agency
        r = search.search_jobs(s, None, browse=True, exclude_agencies=True)
        assert [h.job.company for h in r["hits"]] == ["Acme"]
        assert len(search.search_jobs(s, None, browse=True)["hits"]) == 2
    r = c.patch(
        f"/admin/boards/{a}", json={"is_agency": False}, headers=admin
    ).json()
    assert r["is_agency"] is False and r["agency_override"] is False
    sync.sync_board(
        a, lambda v, s: [fj("Engineer", board=s, vendor=v, url="https://x/a")]
    )
    with db.session() as s:
        assert not s.get(
            Board, a
        ).is_agency  # the admin's call survives a sync
    r = c.patch(
        f"/admin/boards/{a}", json={"clear_agency": True}, headers=admin
    ).json()
    assert r["is_agency"] is True and r["agency_override"] is None
    hit = c.get("/public/jobs?exclude_agencies=true").json()["hits"]
    assert all(h["agency"] is False for h in hit)


def test_preferences_use_the_new_filters():
    _stored()
    uid = mk_user("p@x.io")
    with db.session() as s:
        from livejobs.orm import User

        u = s.get(User, uid)
        p = prefs.update_preferences(
            s,
            u,
            {
                "title_include": ["engineer*", "director", "specialist"],
                "seniority": ["senior", "executive"],
                "countries": ["us", "gb"],
                "exclude_agencies": True,
            },
        )
        assert prefs.prefs_dict(p)["countries"] == ["US", "GB"]
        jobs, _ = matching.find_matches(s, uid, p)
        assert sorted(j.title for j in jobs) == [
            "Director of Sales",
            "Senior Backend Engineer",
        ]
        p.remote_only = True
        jobs, _ = matching.find_matches(s, uid, p)
        assert [j.title for j in jobs] == ["Senior Backend Engineer"]
        with pytest.raises(Exception, match="seniority"):
            prefs.update_preferences(s, u, {"seniority": ["boss"]})


def test_migration_adds_and_backfills_the_label_columns():
    from sqlalchemy import text

    _stored()
    eng = db.engine()
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN workplace"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN seniority"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN country"))
        conn.execute(text("ALTER TABLE boards DROP COLUMN is_agency"))
        conn.execute(text("ALTER TABLE boards DROP COLUMN agency_override"))
        conn.execute(text("ALTER TABLE preferences DROP COLUMN seniority"))
        conn.execute(text("ALTER TABLE preferences DROP COLUMN countries"))
        conn.execute(
            text("ALTER TABLE preferences DROP COLUMN exclude_agencies")
        )
    assert not migrations.is_current()
    changed = migrations.ensure_schema()
    assert (
        any("rows labelled" in x for x in changed) and migrations.is_current()
    )
    with db.session() as s:
        rows = {j.title: j for j in s.scalars(select(Job))}
    assert rows["Senior Backend Engineer"].workplace == "remote"
    assert rows["Director of Sales"].seniority == "executive"
    assert migrations.ensure_schema() == []  # and it is idempotent


def test_backfill_works_in_batches():
    _stored()
    with db.session() as s:
        for j in s.scalars(select(Job)):
            j.workplace = j.seniority = j.country = ""
    assert migrations.backfill_job_labels(db.engine(), batch=2) == 4
    with db.session() as s:
        assert {j.country for j in s.scalars(select(Job))} == {
            "US",
            "DE",
            "GB",
            "CA",
        }


# ── newest first ────────────────────────────────────────────────────────
def _dated(c=None):
    from datetime import timedelta as td

    now = utcnow()
    b = mk_board("lever", "dated")
    stamp = lambda d: (now - td(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    sync.sync_board(
        b,
        lambda v, s: [
            fj("Old one", board=s, vendor=v, posted=stamp(30)),
            fj("Newest one", board=s, vendor=v, posted=stamp(1)),
            fj("Middle one", board=s, vendor=v, posted=stamp(10)),
            fj("Undated one", board=s, vendor=v, posted=""),
            fj("Future one", board=s, vendor=v, posted=stamp(-400)),
        ],
    )
    return b


def test_job_lists_are_newest_first_by_publish_date():
    _dated()
    with db.session() as s:
        r = search.search_jobs(s, None, browse=True)
        titles = [h.job.title for h in r["hits"]]
    # Undated jobs sort by when we first saw them (just now); a date in the
    # future is not allowed to pin a job to the top of the list forever.
    assert titles.index("Newest one") < titles.index("Middle one")
    assert titles.index("Middle one") < titles.index("Old one")
    assert titles[-1] == "Old one"
    assert {"Undated one", "Future one"} <= set(titles[:2])


def test_sort_at_is_never_in_the_future_and_falls_back_to_first_seen():
    _dated()
    now = utcnow()
    with db.session() as s:
        rows = {j.title: j for j in s.scalars(select(Job))}
    assert rows["Future one"].sort_at <= now
    assert rows["Undated one"].posted_at is None
    assert rows["Undated one"].sort_at == rows["Undated one"].first_seen
    assert rows["Newest one"].sort_at == rows["Newest one"].posted_at


def test_a_later_publish_date_is_picked_up_for_an_undated_job():
    b = mk_board("lever", "late")
    sync.sync_board(b, lambda v, s: [fj("Role", board=s, vendor=v, posted="")])
    sync.sync_board(
        b,
        lambda v, s: [
            fj("Role", board=s, vendor=v, posted="2026-01-02T00:00:00Z")
        ],
    )
    with db.session() as s:
        j = s.scalar(select(Job))
        assert j.posted_at.year == 2026 and j.sort_at == j.posted_at


def test_keyword_search_can_be_relevance_or_strictly_newest():
    from datetime import timedelta as td

    now = utcnow()
    b = mk_board("lever", "kw")
    st = lambda d: (now - td(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    sync.sync_board(
        b,
        lambda v, s: [
            fj("Python Engineer", board=s, vendor=v, posted=st(20), desc="x"),
            fj(
                "Support",
                board=s,
                vendor=v,
                posted=st(1),
                desc="we use python",
            ),
            fj("Python Lead", board=s, vendor=v, posted=st(5), desc="x"),
        ],
    )
    with db.session() as s:

        def order(**kw):
            r = search.search_jobs(s, None, keywords="python", **kw)
            return [h.job.title for h in r["hits"]]

        # relevance: title hits first (newest among them), then the rest
        assert order() == ["Python Lead", "Python Engineer", "Support"]
        assert order(sort="relevance") == order()
        # newest: purely by date
        assert order(sort="newest") == [
            "Support",
            "Python Lead",
            "Python Engineer",
        ]
        with pytest.raises(Exception, match="sort must be"):
            order(sort="best")


def test_public_jobs_default_to_newest_and_expose_listed_at(c):
    _dated()
    r = c.get("/public/jobs").json()
    titles = [h["title"] for h in r["hits"]]
    assert titles.index("Newest one") < titles.index("Old one")
    assert all(h["listed_at"] for h in r["hits"])
    old_first = c.get("/public/jobs?sort=relevance").json()["hits"]
    assert old_first  # still accepted; browse ordering is by date either way
    assert c.get("/public/jobs?sort=best").status_code == 400


def test_migration_adds_and_fills_sort_at_with_its_index():
    from sqlalchemy import inspect, text

    _dated()
    eng = db.engine()
    with eng.begin() as conn:
        conn.execute(text("DROP INDEX ix_jobs_active_sort ON jobs"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN sort_at"))
    assert not migrations.is_current()
    changed = migrations.ensure_schema()
    assert any("sort_at" in x for x in changed) and migrations.is_current()
    assert "ix_jobs_active_sort" in {
        i["name"] for i in inspect(eng).get_indexes("jobs")
    }
    with db.session() as s:
        rows = {j.title: j for j in s.scalars(select(Job))}
    assert rows["Newest one"].sort_at == rows["Newest one"].posted_at
    assert rows["Future one"].sort_at <= utcnow()  # clamped by the backfill
    assert rows["Undated one"].sort_at == rows["Undated one"].first_seen
