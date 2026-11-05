"""A faithful local stand-in for the Switch agent bridge, for demos and tests.

It speaks the same wire protocol SNITCH uses against real Switch (register,
SSE events with sequences and Last-Event-ID resume, connection_state, beat with
404/409, ops, connectionless message), plus a small web chat at / so a human
can play on-call. Point SWITCH_URL at real Switch and none of this is used.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

ROOM_NAME = "snitch-checkout-service-incidents"


@dataclass
class MockAgent:
    id: str
    name: str
    api_key: str
    buffer: list[dict[str, Any]] = field(default_factory=list)
    streams: dict[str, asyncio.Queue] = field(default_factory=dict)  # connection_id -> queue
    filters: dict[str, str] = field(default_factory=dict)
    beats: dict[str, float] = field(default_factory=dict)


class Bridge:
    def __init__(self, token: str) -> None:
        self.token = token
        self.room_id = str(uuid.uuid4())
        self.agents: dict[str, MockAgent] = {}
        self.messages: list[dict[str, Any]] = []
        self.documents: dict[str, dict[str, Any]] = {}
        self.seq = 0
        self.human_waiters: list[asyncio.Event] = []

    def agent_by_key(self, agent_id: str, auth: str | None) -> MockAgent:
        a = self.agents.get(agent_id)
        if not a or auth != f"Bearer {a.api_key}":
            raise HTTPException(status_code=401, detail="bad agent credentials")
        return a

    def post(self, sender: str, body: str, sender_kind: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        msg = {
            "id": f"$evt{len(self.messages) + 1}", "kind": "message",
            "sender": f"@{sender}:localhost", "sender_name": sender, "body": body,
            "timestamp": int(time.time() * 1000), "thread_id": thread_id, "attachments": [],
        }
        self.messages.append(msg)
        for a in self.agents.values():
            if a.name == sender:
                continue
            self.seq += 1
            payload = {**msg, "message_id": msg["id"], "addressed": f"@{a.name}" in body}
            if sender_kind:
                payload["sender_kind"] = sender_kind
            event = {"type": "message", "room_id": self.room_id, "bridge_id": "mock",
                     "channel_type": "channel_private", "payload": payload, "sequence": self.seq}
            a.buffer.append(event)
            for cid, q in a.streams.items():
                if a.filters.get(cid) == "addressed" and not payload["addressed"]:
                    continue
                q.put_nowait(event)
        for w in self.human_waiters:
            w.set()
        return msg


def create_app(token: str | None = None) -> FastAPI:
    import os

    bridge = Bridge(token or os.environ.get("SWITCH_REGISTRATION_TOKEN", "snitch-local-token"))
    app = FastAPI(title="mock-switch")
    app.state.bridge = bridge

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "mock": True}

    @app.post("/agents/register-known")
    async def register(req: Request, authorization: str | None = Header(None)) -> dict[str, str]:
        if authorization != f"Bearer {bridge.token}":
            raise HTTPException(status_code=401, detail="bad registration token")
        body = await req.json()
        name = body["name"]
        existing = next((a for a in bridge.agents.values() if a.name == name), None)
        if existing and not body.get("overwrite"):
            raise HTTPException(status_code=409, detail="agent exists")
        if existing:
            del bridge.agents[existing.id]
        a = MockAgent(id=str(uuid.uuid4()), name=name, api_key="sk-" + secrets.token_hex(16))
        bridge.agents[a.id] = a
        return {"id": a.id, "api_key": a.api_key}

    @app.get("/agents/{agent_id}/events")
    async def events(agent_id: str, request: Request, connection_id: str, scope: str = "all",
                     filter: str = "all", start_from: str = "head",
                     authorization: str | None = Header(None),
                     last_event_id: str | None = Header(None)) -> StreamingResponse:
        a = bridge.agent_by_key(agent_id, authorization)
        if old := a.streams.get(connection_id):
            old.put_nowait({"__control": "evicted", "reason": "takeover"})
        q: asyncio.Queue = asyncio.Queue()
        a.streams[connection_id] = q
        a.filters[connection_id] = filter
        a.beats[connection_id] = time.time()
        resume = last_event_id or (start_from if start_from != "head" else None)
        cursor = int(resume) if resume else bridge.seq
        backlog = [e for e in a.buffer if e["sequence"] > cursor
                   and (filter != "addressed" or e["payload"].get("addressed"))]

        async def gen():
            state = {"connection_id": connection_id, "agent_id": a.id, "scope": scope, "filter": filter,
                     "spawn_capable": False, "rooms": [bridge.room_id], "cursor": cursor, "protocol": 6,
                     "heartbeat_interval_seconds": 2, "server": {"name": "mock-switch"}}
            yield f"event: connection_state\ndata: {json.dumps(state)}\n\n"
            for e in backlog:
                yield f"id: {e['sequence']}\nevent: message\ndata: {json.dumps(e)}\n\n"
            try:
                while True:
                    try:
                        e = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if e.get("__control") == "evicted":
                        yield f"event: evicted\ndata: {json.dumps({'reason': e['reason']})}\n\n"
                        return
                    yield f"id: {e['sequence']}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n"
            finally:
                if a.streams.get(connection_id) is q:
                    del a.streams[connection_id]

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/agents/{agent_id}/connection/beat")
    async def beat(agent_id: str, req: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
        a = bridge.agent_by_key(agent_id, authorization)
        body = await req.json()
        cid = body.get("connection_id")
        if cid not in a.beats:
            raise HTTPException(status_code=404, detail="unknown connection")
        if cid not in a.streams:
            raise HTTPException(status_code=409, detail="no stream attached")
        a.beats[cid] = time.time()
        return {"ok": True, "rooms": [bridge.room_id], "cursor": body.get("cursor")}

    @app.post("/agents/{agent_id}/message")
    async def message(agent_id: str, req: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
        a = bridge.agent_by_key(agent_id, authorization)
        body = await req.json()
        if body.get("room_id") != bridge.room_id:
            raise HTTPException(status_code=404, detail="unknown room")
        msg = bridge.post(a.name, body["content"])
        return {"ok": True, "event_id": msg["id"]}

    @app.get("/agents/{agent_id}/ops")
    def ops(agent_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        bridge.agent_by_key(agent_id, authorization)
        names = ["list_rooms", "connect_to_room", "post_message", "read_context",
                 "create_room_document", "load_internal_documents"]
        return {"operations": {n: {"description": n, "input_schema": {}} for n in names}}

    @app.post("/agents/{agent_id}/ops/{op}")
    async def call_op(agent_id: str, op: str, req: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
        a = bridge.agent_by_key(agent_id, authorization)
        args = await req.json()
        if op == "list_rooms":
            return {"result": [{"id": bridge.room_id, "name": ROOM_NAME}]}
        if op == "connect_to_room":
            return {"result": {"room_id": bridge.room_id, "name": ROOM_NAME, "participants": [x.name for x in bridge.agents.values()]}}
        if op == "post_message":
            m = bridge.post(a.name, args["body"], thread_id=args.get("thread_id"))
            return {"result": {"event_id": m["id"]}}
        if op == "read_context":
            limit = int(args.get("limit", 50))
            return {"result": {"threads": [{"root": m, "replies": []} for m in bridge.messages[-limit:]],
                               "truncated": len(bridge.messages) > limit, "oldest_timestamp": None}}
        if op == "create_room_document":
            bridge.documents[args["name"]] = {**args, "author": a.name, "at": time.time()}
            return {"result": {"id": args["name"], "name": args["name"]}}
        if op == "load_internal_documents":
            return {"result": list(bridge.documents.values())}
        raise HTTPException(status_code=404, detail=f"unknown operation {op}")

    # ---- human side ---------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def chat() -> str:
        return resources.files("snitch").joinpath("static/room.html").read_text(encoding="utf-8")

    @app.get("/api/room")
    async def room(after: int = 0, wait: float = 0) -> dict[str, Any]:
        if wait and len(bridge.messages) <= after:
            ev = asyncio.Event()
            bridge.human_waiters.append(ev)
            try:
                await asyncio.wait_for(ev.wait(), timeout=min(wait, 25))
            except asyncio.TimeoutError:
                pass
            finally:
                bridge.human_waiters.remove(ev)
        return {"name": ROOM_NAME, "room_id": bridge.room_id,
                "members": [x.name for x in bridge.agents.values()],
                "messages": bridge.messages[after:], "total": len(bridge.messages),
                "documents": list(bridge.documents.values())}

    @app.post("/api/say")
    async def say(req: Request) -> dict[str, Any]:
        body = await req.json()
        text = str(body.get("body", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty message")
        return bridge.post(str(body.get("name") or "oncall")[:32], text)

    return app
