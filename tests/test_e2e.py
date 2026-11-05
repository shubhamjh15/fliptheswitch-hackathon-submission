"""Full incident, end to end, over real HTTP and SSE: break prod, agents
investigate, a human approves in the room, rollback, postmortem document."""

import asyncio
import socket

import httpx
import pytest
import uvicorn

from snitch.cli import do_register
from snitch.config import Settings
from snitch.mock_switch import create_app as mock_app
from snitch.runtime import run
from snitch.victim import create_app as victim_app


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


async def wait_for(http, predicate, timeout=60):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        room = (await http.get("/api/room")).json()
        if predicate(room):
            return room
        await asyncio.sleep(0.5)
    bodies = "\n---\n".join(m["sender_name"] + ": " + m["body"] for m in room["messages"])
    pytest.fail(f"timed out; room so far:\n{bodies}")


async def test_full_incident(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sp, vp, ap = free_port(), free_port(), free_port()
    s = Settings(switch_url=f"http://127.0.0.1:{sp}", registration_token="t", state_dir=tmp_path,
                 service_url=f"http://127.0.0.1:{vp}", alert_port=ap, poll_seconds=0.5, approvers=["priya"])
    servers = [await serve(mock_app("t"), sp), await serve(victim_app(), vp)]
    await do_register(s)
    agents = asyncio.create_task(run(s))
    try:
        async with httpx.AsyncClient(base_url=s.switch_url, timeout=30) as room, \
                   httpx.AsyncClient(base_url=s.service_url) as svc:
            await asyncio.sleep(3)
            await svc.post("/deploy", json={"version": "v2.14.0", "actor": "ci"})

            r = await wait_for(room, lambda r: any("Rollback plan" in m["body"] for m in r["messages"]))
            text = "\n".join(m["body"] for m in r["messages"])
            assert "INC-1 opened" in text
            assert "SNITCHING" in text and "v2.14.0" in text and "c0ffee7" in text

            # an unauthorised approver is refused
            await room.post("/api/say", json={"name": "mallory", "body": "@snitch-fixer approve INC-1"})
            await wait_for(room, lambda r: any("approver list" in m["body"] for m in r["messages"]))
            assert (await svc.get("/health")).json()["version"] == "v2.14.0"

            await room.post("/api/say", json={"name": "priya", "body": "@snitch-fixer approve INC-1"})
            r = await wait_for(room, lambda r: bool(r["documents"]), timeout=90)
            assert (await svc.get("/health")).json()["version"] == "v2.13.2"
            assert r["documents"][0]["name"] == "postmortem-inc-1"
            assert "INC-1 resolved" in "\n".join(m["body"] for m in r["messages"])
    finally:
        agents.cancel()
        for server, task in servers:
            server.should_exit = True
            await task
