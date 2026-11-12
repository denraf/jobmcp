import threading
import time

import requests

from jobmcp import http


def test_gate_spaces_requests_across_threads():
    g, stamps = http.Gate("x", min_interval=0.05), []

    def hit():
        g.wait()
        stamps.append(time.monotonic())

    ts = [threading.Thread(target=hit) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stamps.sort()
    assert all(
        b - a >= 0.04 for a, b in zip(stamps, stamps[1:])
    )  # even though five threads asked at once


def test_a_failure_pauses_every_thread_and_grows_until_success():
    g = http.Gate("x", min_interval=0, max_cooldown=100)
    first, second = g.failed(), g.failed()
    assert (
        9.9 <= first <= 12.6 and second > first
    )  # 5s * 2**n with jitter: 10s, then 20s
    assert (
        g.failed(hint=90) >= 90
    )  # the server's own Retry-After wins when larger
    g.ok()
    assert 9.9 <= g.failed() <= 12.6  # a success resets the escalation


def test_request_retries_through_a_gate_and_reports_success(monkeypatch):
    seq = iter([503, 200])
    slept = []

    class R:
        def __init__(self, code):
            self.status_code, self.headers = code, {}

    monkeypatch.setattr(http._session, "request", lambda *a, **k: R(next(seq)))
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    g = http.Gate("x", min_interval=0)
    assert (
        http.get("https://example.test/", retries=3, gate=g).status_code == 200
    )
    assert (
        slept and slept[0] >= 10
    )  # backed off hard after the 503, not 2 seconds
    assert g._failures == 0


def test_connection_refused_also_backs_off_together(monkeypatch):
    calls = iter([requests.ConnectionError("refused"), 200])
    slept = []

    class R:
        status_code, headers = 200, {}

    def fake(*a, **k):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return R()

    monkeypatch.setattr(http._session, "request", fake)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    g = http.Gate("x", min_interval=0)
    assert (
        http.get("https://example.test/", retries=2, gate=g).status_code == 200
    )
    assert slept[0] >= 10


def test_common_crawl_has_its_own_threads_session_and_gate(monkeypatch):
    from jobmcp import db, inventory

    assert http.crawl_session is not http._session
    assert http.crawl_session.get_adapter(
        "https://x/"
    ) is not http._session.get_adapter("https://x/")
    seen = set()

    def fetch(crawl, url, match, page):
        seen.add(threading.current_thread().name)
        return ["https://boards.greenhouse.io/acme"]

    from jobmcp.orm import Base

    db.create_schema()
    try:
        inventory.build(
            ["CC-X"],
            ["greenhouse"],
            2,
            lambda m: None,
            fetch=fetch,
            pages_of=lambda c, u, m: 3,
        )
    finally:
        Base.metadata.drop_all(db.engine())
    assert seen and all(
        n.startswith("crawl") for n in seen
    )  # not the "sync" pool's threads


def test_gate_opens_after_consecutive_failures_and_a_success_closes_it():
    g = http.Gate("x", min_interval=0, trip_after=3)
    g.failed(), g.failed()
    assert not g.tripped()
    g.failed()
    assert g.tripped()
    g.ok()
    assert not g.tripped()
    assert not http.Gate("y", trip_after=0).tripped()  # 0 = never give up


def test_request_refuses_at_once_while_the_gate_is_open(monkeypatch):
    g = http.Gate("x", min_interval=0, trip_after=1)
    g.failed()
    calls = []
    monkeypatch.setattr(
        http._session, "request", lambda *a, **k: calls.append(a)
    )
    try:
        http.get("https://x/", gate=g)
    except http.Unavailable:
        pass
    else:
        raise AssertionError("expected Unavailable")
    assert calls == []  # not a single request was sent


def test_a_crawl_stops_when_common_crawl_is_unreachable(monkeypatch):
    from jobmcp import inventory

    monkeypatch.setattr(inventory._gate, "trip_after", 3)
    queried, messages = [], []

    def pages_of(crawl, url, match):
        queried.append(url)
        inventory._gate.failed()  # what http.request does on each failure
        raise requests.ConnectionError("refused")

    stats = inventory.build(
        ["CC-X"], None, 2, messages.append, pages_of=pages_of
    )
    inventory._gate.reset()
    assert stats["gave_up"] and len(queried) == 3  # not every query
    assert any("stopping" in m for m in messages)
