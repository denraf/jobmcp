import threading
import time

import pytest
from fastapi.testclient import TestClient

from jobmcp import cache, config, services
from jobmcp.site import app

from .helpers import fj, mk_board
from jobmcp.services import sync


def test_a_value_is_reused_until_it_expires():
    c, calls = cache.TTLCache(), []

    def compute():
        calls.append(1)
        return len(calls)

    assert c.get_or_compute("k", compute, 0.3) == 1
    assert c.get_or_compute("k", compute, 0.3) == 1  # reused
    time.sleep(0.35)
    assert c.get_or_compute("k", compute, 0.3) == 2  # expired: recomputed
    assert (c.hits, c.misses) == (1, 2)


def test_ttl_zero_turns_the_cache_off():
    c, calls = cache.TTLCache(), []
    for _ in range(3):
        c.get_or_compute("k", lambda: calls.append(1), 0)
    assert len(calls) == 3


def test_different_keys_do_not_share_answers():
    c = cache.TTLCache()
    assert c.get_or_compute(("a", 1), lambda: "A", 5) == "A"
    assert c.get_or_compute(("a", 2), lambda: "B", 5) == "B"


def test_identical_requests_at_the_same_moment_compute_once():
    c, calls, out = cache.TTLCache(), [], []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return "answer"

    ts = [
        threading.Thread(
            target=lambda: out.append(c.get_or_compute("k", slow, 5))
        )
        for _ in range(8)
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and out == ["answer"] * 8


def test_a_failed_computation_is_not_cached():
    c = cache.TTLCache()

    def boom():
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        c.get_or_compute("k", boom, 5)
    assert c.get_or_compute("k", lambda: "ok", 5) == "ok"


def test_the_cache_is_bounded():
    c = cache.TTLCache(maxsize=3)
    for i in range(10):
        c.get_or_compute(i, lambda: i, 60)
    assert len(c._data) == 3


@pytest.fixture()
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "CACHE_SECONDS", 30)
    cache.public.clear()
    return TestClient(app)


def test_the_public_list_is_cached_and_says_so(client, monkeypatch):
    sync.sync_board(mk_board(), lambda v, s: [fj("Data Engineer")])
    calls = []
    real = services.search.search_jobs
    monkeypatch.setattr(
        services.search,
        "search_jobs",
        lambda *a, **k: calls.append(1) or real(*a, **k),
    )
    a = client.get("/api/public/jobs?limit=10")
    b = client.get("/api/public/jobs?limit=10")
    other = client.get("/api/public/jobs?limit=11")
    assert a.status_code == 200 and a.json() == b.json()
    assert len(calls) == 2  # the repeat was answered from the cache
    assert a.json()["total"] == 1 and other.json()["total"] == 1
    cc = a.headers["cache-control"]
    assert "public" in cc and "max-age=30" in cc


def test_with_the_cache_off_nothing_is_kept_or_advertised(
    fresh_db, monkeypatch
):
    monkeypatch.setattr(config, "CACHE_SECONDS", 0)
    r = TestClient(app).get("/api/public/jobs")
    assert r.status_code == 200 and "cache-control" not in r.headers


def test_signed_in_answers_are_never_marked_public(client):
    r = client.get("/api/me")
    assert r.status_code == 401 and "public" not in r.headers.get(
        "cache-control", ""
    )


def test_public_pages_are_compressed_and_private_ones_are_not(client):
    sync.sync_board(
        mk_board(), lambda v, s: [fj("Engineer", desc="word " * 2000)]
    )
    h = {"Accept-Encoding": "gzip"}
    jobs = client.get("/api/public/jobs/1", headers=h)
    assert jobs.status_code == 200
    assert jobs.headers.get("content-encoding") == "gzip"
    assert "word" in jobs.json()["description"]  # and it still decodes
    page = client.get("/jobs", headers=h)
    assert page.headers.get("content-encoding") == "gzip" or (
        len(page.content) < 1024  # too small to be worth compressing
    )
    private = client.get("/api/me", headers=h)
    assert "content-encoding" not in private.headers
