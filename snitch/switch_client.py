"""A from-scratch client for the Switch agent protocol.

SSE down, HTTP up. Implements what the protocol spec asks of a client:
a persistent connection id reused across reconnects, `Last-Event-ID` resume,
a 2-second heartbeat that triggers a reopen on 404/409, `gap` frames held
and attached to the next event, `evicted` handling, and operations invoked
unwrapped with the connection-id header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

log = logging.getLogger("snitch.switch")

DOMAIN_EVENTS = {
    "message", "command", "room_join",
    "task_delegate", "task_accept", "task_update", "task_finalise", "task_cancel",
}


@dataclass
class SSEFrame:
    event: str
    data: dict[str, Any]
    id: int | None = None


@dataclass
class RoomEvent:
    """A domain event surfaced to an agent handler."""

    type: str
    room_id: str
    payload: dict[str, Any]
    sequence: int | None
    gap: dict[str, Any] | None = None

    @property
    def body(self) -> str:
        return str(self.payload.get("body") or "")

    @property
    def sender_name(self) -> str:
        return str(self.payload.get("sender_name") or self.payload.get("sender") or "")

    @property
    def addressed(self) -> bool:
        return bool(self.payload.get("addressed"))

    @property
    def thread_id(self) -> str | None:
        return self.payload.get("thread_id") or self.payload.get("message_id")


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEFrame]:
    """Parse an SSE line stream into frames. Comments (keepalives) are skipped."""
    event, data_lines, frame_id = "message", [], None
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                text = "\n".join(data_lines)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = {"raw": text}
                yield SSEFrame(event=event, data=data, id=frame_id)
            event, data_lines, frame_id = "message", [], None
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data_lines.append(value)
        elif name == "id":
            try:
                frame_id = int(value)
            except ValueError:
                frame_id = None


class Reopen(Exception):
    """Raised inside the session to force the stream to be reopened."""


Handler = Callable[["SwitchAgent", RoomEvent], Awaitable[None]]


@dataclass
class SwitchAgent:
    base_url: str
    agent_id: str
    api_key: str
    name: str
    state_dir: Path = Path(".snitch")
    scope: str = "all"
    filter: str = "addressed"
    rooms: list[str] = field(default_factory=list)
    client_name: str = "snitch"
    client_version: str = "1.0.0"

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self.state_dir / f"{self.name}.conn.json"
        state = self._load_state()
        self.connection_id: str = state.get("connection_id") or str(uuid.uuid4())
        self.cursor: int | None = state.get("cursor")
        self._pending_gap: dict[str, Any] | None = None
        self._seen: set[int] = set()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(30.0, read=None),
        )
        self._save_state()

    # ---- persistence -------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        self._state_file.write_text(
            json.dumps({"connection_id": self.connection_id, "cursor": self.cursor}),
            encoding="utf-8",
        )

    # ---- operations --------------------------------------------------
    async def op(self, operation: str, **args: Any) -> Any:
        resp = await self._http.post(
            f"/agents/{self.agent_id}/ops/{operation}",
            json=args,
            headers={"X-Switch-Connection-Id": self.connection_id},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"{operation} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("result")

    async def say(self, room_id: str, content: str) -> None:
        """Post into a room. Uses the connectionless message route so any agent can
        speak in any room it belongs to without stealing another session's claim."""
        resp = await self._http.post(
            f"/agents/{self.agent_id}/message",
            json={"room_id": room_id, "content": content},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"message -> {resp.status_code}: {resp.text[:300]}")

    async def mention(self, room_id: str, names: list[str], content: str) -> None:
        prefix = " ".join(f"@{n}" for n in names)
        await self.say(room_id, f"{prefix} {content}".strip())

    async def connect_room(self, room_id: str) -> dict[str, Any]:
        return await self.op("connect_to_room", room_id=room_id, include_general_instructions=False)

    async def history(self, room_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Flatten read_context threads into a chronological message list."""
        result = await self.op("read_context", limit=limit, room_id=room_id) or {}
        messages: list[dict[str, Any]] = []
        for thread in result.get("threads", []):
            for key in ("root", "message"):
                if isinstance(thread.get(key), dict):
                    messages.append(thread[key])
            for m in thread.get("messages", []) + thread.get("replies", []):
                if isinstance(m, dict):
                    messages.append(m)
        seen, out = set(), []
        for m in messages:
            mid = m.get("id") or m.get("message_id") or id(m)
            if mid in seen:
                continue
            seen.add(mid)
            out.append(m)
        out.sort(key=lambda m: m.get("timestamp") or 0)
        return out

    # ---- the stream --------------------------------------------------
    def _events_params(self) -> dict[str, str]:
        params = {
            "connection_id": self.connection_id,
            "scope": self.scope,
            "filter": self.filter,
            "start_from": str(self.cursor) if self.cursor is not None else "head",
            "client": self.client_name,
            "client_version": self.client_version,
        }
        if self.rooms:
            params["rooms"] = ",".join(self.rooms)
        return params

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._beat_interval)
            body: dict[str, Any] = {"connection_id": self.connection_id}
            if self.cursor is not None:
                body["cursor"] = self.cursor
            try:
                resp = await self._http.post(
                    f"/agents/{self.agent_id}/connection/beat", json=body, timeout=5.0
                )
            except httpx.HTTPError as exc:
                log.warning("%s heartbeat failed: %s", self.name, exc)
                continue
            if resp.status_code in (404, 409):
                # Both mean reopen, not back off.
                raise Reopen(f"heartbeat {resp.status_code}")

    async def _consume(self, handler: Handler) -> None:
        headers = {"Accept": "text/event-stream"}
        if self.cursor is not None:
            headers["Last-Event-ID"] = str(self.cursor)
        async with self._http.stream(
            "GET", f"/agents/{self.agent_id}/events", params=self._events_params(), headers=headers
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="replace")[:300]
                raise RuntimeError(f"open stream -> {resp.status_code}: {body}")
            async for frame in parse_sse(resp.aiter_lines()):
                await self._on_frame(frame, handler)

    async def _on_frame(self, frame: SSEFrame, handler: Handler) -> None:
        kind = frame.event or frame.data.get("type", "")
        if kind == "connection_state":
            self._beat_interval = float(frame.data.get("heartbeat_interval_seconds") or 2)
            if frame.data.get("cursor") is not None:
                self.cursor = int(frame.data["cursor"])
            log.info("%s connected: rooms=%s cursor=%s", self.name, frame.data.get("rooms"), self.cursor)
            return
        if kind == "gap":
            log.warning("%s gap: %s", self.name, frame.data)
            self._pending_gap = frame.data  # not a wake; attach to the next event
            return
        if kind == "evicted":
            raise Reopen(f"evicted: {frame.data.get('reason')}")
        if kind in ("subscription_changed", "room_released"):
            log.info("%s %s: %s", self.name, kind, frame.data)
            return
        if kind not in DOMAIN_EVENTS:
            return
        seq = frame.data.get("sequence", frame.id)
        if seq is not None:
            seq = int(seq)
            if seq in self._seen:
                return
            self._seen.add(seq)
            if len(self._seen) > 5000:
                self._seen = set(sorted(self._seen)[-2500:])
        event = RoomEvent(
            type=kind,
            room_id=frame.data.get("room_id", ""),
            payload=frame.data.get("payload", {}),
            sequence=seq,
            gap=self._pending_gap,
        )
        self._pending_gap = None
        try:
            await handler(self, event)
        except Exception:  # a bad event must never kill the agent
            log.exception("%s handler failed on %s", self.name, kind)
        if seq is not None:
            self.cursor = max(self.cursor or 0, seq)
            self._save_state()

    async def run(self, handler: Handler) -> None:
        """Hold the connection forever: open, beat, reopen on any failure."""
        backoff = 1.0
        while True:
            self._beat_interval = 2.0
            stream = asyncio.create_task(self._consume(handler))
            beat = asyncio.create_task(self._heartbeat())
            done, pending = await asyncio.wait({stream, beat}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            exc = next((t.exception() for t in done if not t.cancelled() and t.exception()), None)
            if isinstance(exc, Reopen):
                log.info("%s reopening stream (%s)", self.name, exc)
                backoff = 1.0
                continue
            log.warning("%s stream ended (%s); retrying in %.0fs", self.name, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def aclose(self) -> None:
        await self._http.aclose()


async def register_agent(
    base_url: str, token: str, name: str, description: str, agent_type: str = "codex",
    display_name: str | None = None, overwrite: bool = True,
) -> dict[str, str]:
    """Register once, out of band, with the registration token."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        resp = await http.post(
            "/agents/register-known",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "agent_type": agent_type,
                "name": name,
                "description": description,
                "display_name": display_name,
                "options": {},
                "overwrite": overwrite,
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"register {name} -> {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return {"id": data["id"], "api_key": data["api_key"], "name": name}
