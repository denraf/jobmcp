"""End to end over real HTTP: a live uvicorn serving the MCP app, driven by the
official MCP client."""

import asyncio
import json
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from jobmcp import db, mcp_server
from jobmcp.orm import Application
from jobmcp.services import auth, prefs, sync
from sqlalchemy import select

from .helpers import fj, mk_board, mk_user

pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture()
def server():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    srv = uvicorn.Server(
        uvicorn.Config(
            mcp_server.http_app(),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    t.join(5)


def token_for(uid):
    with db.session() as s:
        return auth.issue_token(s, s.get(auth.User, uid), "mcp")[0]


def call(url, token, tool, args=None):
    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with Client(streamable_http_client(url, http_client=http)) as c:
            r = await c.call_tool(tool, args or {})
            return r

    return asyncio.run(go())


async def _tools(url, token):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    async with Client(streamable_http_client(url, http_client=http)) as c:
        return (await c.list_tools()).tools


def payload(r):
    if r.is_error:
        return {"error": r.content[0].text}
    d = (
        r.structured_content
        if r.structured_content is not None
        else json.loads(r.content[0].text)
    )
    return (
        d["result"] if isinstance(d, dict) and list(d) == ["result"] else d
    )  # MCP wraps bare lists


def seed():
    u1, u2 = mk_user("a@x.io"), mk_user("b@x.io")
    sync.sync_board(mk_board(), lambda v, s: [fj("Data Engineer"), fj("SRE")])
    for u in (u1, u2):
        with db.session() as s:
            prefs.update_preferences(
                s, s.get(auth.User, u), {"title_include": ["data engineer"]}
            )
    return u1, u2


def test_rejects_missing_bad_and_wrong_kind_tokens(server):
    u1, _ = seed()
    import requests

    assert requests.post(server, json={}).status_code == 401
    assert (
        requests.post(
            server, json={}, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    with db.session() as s:
        sess, _ = auth.issue_token(s, s.get(auth.User, u1), "session")
    assert (
        requests.post(
            server, json={}, headers={"Authorization": f"Bearer {sess}"}
        ).status_code
        == 401
    )


def test_full_apply_flow_and_user_isolation(server):
    u1, u2 = seed()
    t1, t2 = token_for(u1), token_for(u2)

    me = payload(call(server, t1, "get_my_preferences"))
    assert (
        me["preferences"]["title_include"] == ["data engineer"]
        and "email" not in me
        and "profile" not in me
    )
    names = {t.name for t in asyncio.run(_tools(server, t1))}
    assert (
        "get_my_profile" not in names
        and {"save_job", "list_saved_jobs", "unsave_job", "search_jobs"}
        <= names
    )

    matches = payload(call(server, t1, "list_matching_jobs"))["jobs"]
    assert [j["title"] for j in matches] == ["Data Engineer"]
    jid = matches[0]["job_id"]

    started = payload(call(server, t1, "start_application", {"job_id": jid}))
    assert (
        started["claimed"]
        and started["job"]["apply_url"]
        and "profile" not in started
    )
    done = payload(
        call(
            server,
            t1,
            "record_application_result",
            {"job_id": jid, "status": "applied", "notes": "ok"},
        )
    )
    assert done["status"] == "applied"

    # u1 no longer sees it; u2 still does, and u2's ledger is empty
    assert payload(call(server, t1, "list_matching_jobs"))["jobs"] == []
    assert len(payload(call(server, t2, "list_matching_jobs"))["jobs"]) == 1
    assert payload(call(server, t2, "list_my_applications")) == []
    ledger = payload(call(server, t1, "list_my_applications"))
    assert len(ledger) == 1 and ledger[0]["status"] == "applied"

    # double-apply is refused
    again = payload(call(server, t1, "start_application", {"job_id": jid}))
    assert "already applied" in again["error"]

    with db.session() as s:
        rows = s.scalars(select(Application)).all()
        assert [(r.user_id, r.status) for r in rows] == [(u1, "applied")]


def test_revoked_token_stops_working_immediately(server):
    u1, _ = seed()
    with db.session() as s:
        raw, t = auth.issue_token(s, s.get(auth.User, u1), "mcp")
        tid = t.id
    assert payload(call(server, raw, "get_my_preferences"))["preferences"]
    with db.session() as s:
        auth.revoke_token(s, s.get(auth.User, u1), tid)
    with pytest.raises(Exception):
        call(server, raw, "get_my_preferences")


def test_update_preferences_over_mcp(server):
    u1, _ = seed()
    t = token_for(u1)
    out = payload(
        call(
            server,
            t,
            "update_my_preferences",
            {"preferences": {"title_include": ["sre"], "remote_only": True}},
        )
    )
    assert out["title_include"] == ["sre"] and out["remote_only"] is True
    assert [
        j["title"]
        for j in payload(call(server, t, "list_matching_jobs"))["jobs"]
    ] == ["SRE"]


def test_search_over_mcp(server):
    u1, u2 = seed()
    sync.sync_board(
        mk_board("lever", "other"),
        lambda v, s: [
            fj(
                "Platform Engineer",
                board=s,
                vendor="lever",
                desc="We run Kubernetes at scale.",
            )
        ],
    )
    t1, t2 = token_for(u1), token_for(u2)
    r = payload(call(server, t1, "search_jobs", {"keywords": "kubernetes"}))
    assert (
        r["total"] == 1
        and r["hits"][0]["title"] == "Platform Engineer"
        and "Kubernetes" in r["hits"][0]["snippet"]
    )
    r = payload(
        call(server, t1, "search_jobs", {"title": "sre"})
    )  # not in u1's preferences, still searchable
    assert [h["title"] for h in r["hits"]] == ["SRE"]
    jid = r["hits"][0]["job_id"]
    payload(call(server, t1, "start_application", {"job_id": jid}))
    payload(
        call(
            server,
            t1,
            "record_application_result",
            {"job_id": jid, "status": "applied"},
        )
    )
    assert (
        payload(call(server, t1, "search_jobs", {"title": "sre"}))["total"]
        == 0
    )  # hidden once applied
    r = payload(
        call(
            server,
            t1,
            "search_jobs",
            {"title": "sre", "include_applied": True},
        )
    )
    assert r["hits"][0]["my_status"] == "applied"
    assert (
        payload(call(server, t2, "search_jobs", {"title": "sre"}))["total"]
        == 1
    )  # per-user ledger
    assert (
        "give a title" in payload(call(server, t1, "search_jobs", {}))["error"]
    )


def test_saved_jobs_over_mcp_are_per_user(server):
    from jobmcp import metrics

    ok0 = metrics.MCP_CALLS.labels("save_job", "ok")._value.get()
    refused0 = metrics.MCP_CALLS.labels("save_job", "refused")._value.get()
    u1, u2 = seed()
    t1, t2 = token_for(u1), token_for(u2)
    jid = payload(call(server, t1, "search_jobs", {"title": "sre"}))["hits"][
        0
    ]["job_id"]
    assert (
        payload(call(server, t1, "save_job", {"job_id": jid}))["saved"] is True
    )
    saved = payload(call(server, t1, "list_saved_jobs"))
    assert [j["job_id"] for j in saved] == [jid] and saved[0]["open"] is True
    assert payload(call(server, t2, "list_saved_jobs")) == []
    assert (
        "not found"
        in payload(call(server, t1, "save_job", {"job_id": 99999}))["error"]
    )
    assert (
        metrics.MCP_CALLS.labels("save_job", "ok")._value.get() == ok0 + 1
    )  # tool calls are counted by outcome
    assert (
        metrics.MCP_CALLS.labels("save_job", "refused")._value.get()
        == refused0 + 1
    )
    assert (
        payload(call(server, t1, "unsave_job", {"job_id": jid}))["saved"]
        is False
    )
    assert payload(call(server, t1, "list_saved_jobs")) == []


def test_mcp_mounted_inside_the_site(fresh_db):
    """The same MCP endpoint, served at /mcp/ by the combined site app (what
    `jobmcp serve` runs)."""
    from jobmcp import site

    u1, _ = seed()
    token = token_for(u1)
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    srv = uvicorn.Server(
        uvicorn.Config(
            site.app, host="127.0.0.1", port=port, log_level="warning"
        )
    )
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    try:
        r = payload(
            call(f"http://127.0.0.1:{port}/mcp/", token, "get_my_preferences")
        )
        assert r["preferences"]["title_include"] == ["data engineer"]
        import requests

        assert (
            requests.post(f"http://127.0.0.1:{port}/mcp/", json={}).status_code
            == 401
        )
        assert (
            requests.get(
                f"http://127.0.0.1:{port}/api/public/config"
            ).status_code
            == 200
        )
    finally:
        srv.should_exit = True
        t.join(5)


def test_get_job_returns_the_whole_description_over_mcp(server):
    """The reported bug: an agent fetching a job got the description cut off
    mid-sentence, losing the qualifications."""
    u1, _ = seed()
    last = "We look forward to hearing from you, whatever your background."
    long = (
        "About the role. "
        + "You will own a system end to end. " * 400
        + "Qualifications:\n- 5+ years of Python\n- SQL\n"
        + last
    )
    assert len(long) > 10_000
    sync.sync_board(
        mk_board("lever", "longco"),
        lambda v, s: [
            fj("Long Posting Engineer", board=s, vendor="lever", desc=long)
        ],
    )
    t1 = token_for(u1)
    jid = payload(call(server, t1, "search_jobs", {"title": "long posting"}))[
        "hits"
    ][0]["job_id"]
    got = payload(call(server, t1, "get_job", {"job_id": jid}))["description"]
    assert got == long and got.endswith(
        last
    )  # nothing missing, including the qualifications at the end
    started = payload(call(server, t1, "start_application", {"job_id": jid}))
    assert (
        started["job"]["description"] == long
    )  # and the same when the agent claims the job to apply


# What the agent is told about the application form. jobmcp cannot see the
# form, so the instructions are the whole mechanism: pin the topics down.
MUST_ASK = [
    "visa sponsorship",
    "work authorization",
    "disability",
    "veteran",
    "race",
    "gender",
    "custom question",
    "decline to self-identify",
    "never guess",
]


def test_the_agent_is_told_to_ask_the_user_for_every_extra_form_input():
    rules = mcp_server.APPLY_RULES.lower()
    for topic in MUST_ASK:
        assert topic in rules, topic
    assert "never put the user's answers" in rules  # none of it is kept
    assert "skipped" in rules  # unanswered means skip, not guess
    # the same rules reach the agent wherever it reads instructions
    assert mcp_server.APPLY_RULES in mcp_server.mcp.instructions
    assert mcp_server.APPLY_RULES in mcp_server.apply_to_matching_jobs(3)
    for topic in (
        "visa sponsorship",
        "disability",
        "gender",
        "custom question",
    ):
        assert topic in mcp_server.APPLY_REMINDER.lower()


def test_every_claimed_job_carries_the_reminder_and_notes_stay_clean(server):
    u1, _ = seed()
    t1 = token_for(u1)
    jid = payload(call(server, t1, "list_matching_jobs"))["jobs"][0]["job_id"]
    started = payload(call(server, t1, "start_application", {"job_id": jid}))
    assert started["before_you_submit"] == mcp_server.APPLY_REMINDER
    tools = {x.name: x for x in asyncio.run(_tools(server, t1))}
    assert (
        "never put the user's answers"
        in tools["record_application_result"].description.lower()
    )
    assert "ask the user" in tools["start_application"].description.lower()


def test_the_endpoint_answers_to_the_public_host_only(fresh_db, monkeypatch):
    """Behind the reverse proxy the Host header is the real domain. The MCP
    library refuses anything but localhost unless told otherwise (421)."""
    import requests

    from jobmcp import config, mcp_server

    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://jobmcp.ai")
    monkeypatch.setattr(config, "PUBLIC_MCP_URL", "https://jobmcp.ai/mcp/")
    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["www.jobmcp.ai"])
    app = mcp_server.http_app()
    u1, _ = seed()
    token = token_for(u1)
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    srv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)

    def post(host, origin=None):
        h = {
            "Authorization": f"Bearer {token}",
            "Host": host,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if origin:
            h["Origin"] = origin
        return requests.post(
            f"http://127.0.0.1:{port}/mcp",
            headers=h,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        ).status_code

    try:
        assert post("jobmcp.ai") == 200
        assert post("www.jobmcp.ai") == 200  # JOBMCP_ALLOWED_HOSTS
        assert post("jobmcp.ai", "https://jobmcp.ai") == 200
        assert post("evil.example") == 421
        assert post("jobmcp.ai", "https://evil.example") == 403
    finally:
        srv.should_exit = True
        t.join(5)
