"""The four SNITCH agents. Each is its own Switch agent with its own connection;
they coordinate only through the room, by @-mentioning each other, so the
humans in the channel see (and can interrupt) every hand-off."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import brain
from .config import AGENT_NAMES, Settings
from .investigate import correlate, rollback_target, ts
from .switch_client import RoomEvent, SwitchAgent

log = logging.getLogger("snitch.agents")

INC_RE = re.compile(r"\bINC-(\d+)\b", re.I)
BAD_RE = re.compile(r"\bbad=(v[\w.\-]+)", re.I)
OUR_NAMES = set(AGENT_NAMES.values())


def incident_id(text: str) -> str | None:
    m = INC_RE.search(text)
    return f"INC-{m.group(1)}" if m else None


def defang(text: str) -> str:
    """Quote room text without re-addressing anyone (zero-width joiner after @)."""
    return text.replace("@", "@​")


def from_agent(event: RoomEvent) -> bool:
    return event.sender_name.lstrip("@").split(":")[0] in OUR_NAMES or event.payload.get("sender_kind") == "platform"


@dataclass
class Incident:
    id: str
    room_id: str
    opened_at: float
    health: dict[str, Any]
    status: str = "open"  # open -> investigating -> awaiting_approval -> mitigating -> resolved
    bad_version: str | None = None
    target_version: str | None = None
    resolved_at: float | None = None
    notes: list[str] = field(default_factory=list)


class Service:
    """HTTP access to the guarded service (deploy ledger, logs, health, deploy)."""

    def __init__(self, base_url: str) -> None:
        self.http = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    async def get(self, path: str) -> Any:
        r = await self.http.get(path)
        r.raise_for_status()
        return r.json()

    async def health(self) -> dict[str, Any]:
        return await self.get("/health")

    async def deploys(self) -> list[dict[str, Any]]:
        return await self.get("/deploys")

    async def logs(self) -> list[dict[str, Any]]:
        return await self.get("/logs?level=ERROR&limit=200")

    async def deploy(self, version: str, actor: str) -> dict[str, Any]:
        r = await self.http.post("/deploy", json={"version": version, "actor": actor})
        r.raise_for_status()
        return r.json()


class Snitch:
    """Shared runtime: settings, the service, and the incident book."""

    def __init__(self, settings: Settings, agents: dict[str, SwitchAgent]) -> None:
        self.s = settings
        self.agents = agents
        self.svc = Service(settings.service_url)
        self.incidents: dict[str, Incident] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    def name(self, role: str) -> str:
        return AGENT_NAMES[role]

    def open_incident(self) -> Incident | None:
        return next((i for i in self.incidents.values() if i.status != "resolved"), None)

    async def room(self) -> str:
        if self.s.room_id:
            return self.s.room_id
        rooms = await self.agents["sentinel"].op("list_rooms") or []
        if isinstance(rooms, dict):
            rooms = rooms.get("rooms", [])
        for r in rooms:
            if "snitch" in str(r.get("name", "")).lower() or "incident" in str(r.get("name", "")).lower():
                self.s.room_id = r.get("id") or r.get("room_id")
                return self.s.room_id
        if rooms:
            self.s.room_id = rooms[0].get("id") or rooms[0].get("room_id")
            return self.s.room_id
        raise RuntimeError("snitch-sentinel is in no rooms; create the war room first")

    # ------------------------------------------------------------------ sentinel
    async def raise_incident(self, health: dict[str, Any], source: str) -> Incident | None:
        async with self._lock:
            if self.open_incident():
                return None
            self._counter += 1
            inc = Incident(f"INC-{self._counter}", await self.room(), time.time(), health)
            self.incidents[inc.id] = inc
        rate = float(health.get("error_rate", 0)) * 100
        await self.agents["sentinel"].say(
            inc.room_id,
            f"🚨 **{inc.id} opened** · `{health.get('service', self.s.service_name)}` is "
            f"**{health.get('status', 'degraded')}** · error rate **{rate:.0f}%** on "
            f"`{health.get('version', '?')}` (source: {source}, {ts(inc.opened_at)})\n"
            f"@{self.name('detective')} {inc.id}: find out who did this.",
        )
        inc.status = "investigating"
        return inc

    async def sentinel_watch(self) -> None:
        strikes = 0
        while True:
            await asyncio.sleep(self.s.poll_seconds)
            try:
                health = await self.svc.health()
            except httpx.HTTPError as exc:
                log.warning("health probe failed: %s", exc)
                health = {"service": self.s.service_name, "status": "down", "error_rate": 1.0, "version": "?"}
            bad = float(health.get("error_rate", 0)) >= self.s.error_rate_threshold
            strikes = strikes + 1 if bad else 0
            inc = self.open_incident()
            if strikes >= 2 and inc is None:
                await self.raise_incident(health, "health probe")
            elif inc and inc.status == "mitigating" and not bad and health.get("requests_30s", 0) >= 10:
                inc.status = "resolved"
                inc.resolved_at = time.time()
                mins = (inc.resolved_at - inc.opened_at) / 60
                await self.agents["sentinel"].say(
                    inc.room_id,
                    f"✅ **{inc.id} resolved** · error rate back to {float(health['error_rate'])*100:.0f}% "
                    f"on `{health.get('version')}` · time to recover **{mins:.1f} min**.\n"
                    f"@{self.name('scribe')} {inc.id} is closed. Write the postmortem.",
                )

    async def on_sentinel(self, agent: SwitchAgent, ev: RoomEvent) -> None:
        if ev.type != "message" or from_agent(ev):
            return
        body = ev.body.lower()
        if "status" in body or "health" in body:
            h = await self.svc.health()
            inc = self.open_incident()
            await agent.say(ev.room_id, f"📡 `{h['service']}` {h['status']} · {h['version']} · "
                            f"error rate {h['error_rate']*100:.0f}% · open incident: {inc.id + ' (' + inc.status + ')' if inc else 'none'}")
        elif "page" in body or "incident" in body:
            h = await self.svc.health()
            if not await self.raise_incident(h, f"paged by {ev.sender_name}"):
                await agent.say(ev.room_id, "An incident is already open. Ask me for `status`.")

    # ----------------------------------------------------------------- detective
    async def on_detective(self, agent: SwitchAgent, ev: RoomEvent) -> None:
        if ev.type != "message":
            return
        inc_id = incident_id(ev.body)
        inc = self.incidents.get(inc_id) if inc_id else self.open_incident()
        if from_agent(ev) and (not inc or inc.status == "resolved"):
            return
        deploys, logs = await self.svc.deploys(), await self.svc.logs()
        v = correlate(deploys, logs)
        if v.suspect is None:
            await agent.say(ev.room_id, "🕵️ No deploy history to work with. I can't name anyone yet.")
            return
        s = v.suspect
        facts = (
            f"incident={inc.id if inc else 'ad-hoc'} service={self.s.service_name}\n"
            f"suspect deploy: {s['version']} sha={s.get('sha')} author={s.get('author')} "
            f"message={s.get('message')!r} deployed {ts(s['at'])} by {s.get('actor')}\n"
            f"previous good: {v.previous['version'] if v.previous else 'none'}\n"
            f"errors: {v.error_count}, first at {ts(v.first_error_at)}, signature: {v.signature}\n"
            f"evidence: {'; '.join(v.reasons)}\nconfidence: {v.confidence:.0%}\n"
            f"sample trace:\n{logs[-1].get('trace', '') if logs else ''}"
        )
        fallback = (
            f"🐀 **SNITCHING** {inc.id if inc else ''}: it was **`{s['version']}`** "
            f"(`{s.get('sha')}` by **@{s.get('author')}**, \"{s.get('message')}\"), shipped {ts(s['at'])}.\n"
            f"• {v.error_count} errors, first at {ts(v.first_error_at)}: `{v.signature}`\n"
            + "".join(f"• {r}\n" for r in v.reasons)
            + f"• confidence **{v.confidence:.0%}**"
        )
        text = await brain.write(
            "You are snitch-detective, an incident investigator in a team chat. Name the deploy and "
            "commit that broke production, cite evidence, and guess the root cause from the trace. "
            "Start with '🐀 **SNITCHING**'. Under 120 words, markdown bullets. Blameless tone: name the "
            "change, not a villain.",
            facts, fallback, self.s.anthropic_model,
        )
        await agent.say(ev.room_id, text)
        if inc and v.previous and inc.status in ("open", "investigating"):
            inc.bad_version = s["version"]
            inc.notes.append(f"suspect {s['version']} ({v.confidence:.0%})")
            await agent.mention(ev.room_id, [self.name("fixer")],
                                f"{inc.id} bad={s['version']}: prepare a rollback.")

    # --------------------------------------------------------------------- fixer
    async def on_fixer(self, agent: SwitchAgent, ev: RoomEvent) -> None:
        if ev.type != "message":
            return
        body = ev.body
        inc_id = incident_id(body)
        inc = self.incidents.get(inc_id) if inc_id else self.open_incident()
        human = not from_agent(ev)
        verb = body.lower()
        if not human and (not inc or inc.status not in ("open", "investigating")):
            return

        if human and re.search(r"\b(approve|approved|lgtm|ship it|do it)\b", verb):
            if not inc or inc.status != "awaiting_approval":
                await agent.say(ev.room_id, "Nothing is waiting on approval right now.")
                return
            who = ev.sender_name.lstrip("@").split(":")[0].lower()
            if self.s.approvers and who not in self.s.approvers:
                await agent.say(ev.room_id, f"⛔ {ev.sender_name} isn't on the approver list "
                                f"({', '.join(self.s.approvers)}). Still waiting.")
                return
            await self._execute_rollback(agent, inc, ev.sender_name)
            return
        if human and re.search(r"\b(reject|rejected|no|stop|hold)\b", verb):
            if inc and inc.status == "awaiting_approval":
                inc.status = "investigating"
                inc.notes.append(f"rollback rejected by {ev.sender_name}")
                await agent.say(ev.room_id, f"🛑 Standing down on {inc.id}. The rollback was rejected by "
                                f"{ev.sender_name}. Tell me the plan when you have one.")
            return

        m = BAD_RE.search(body)
        if not inc or not m:
            await agent.say(ev.room_id, "Give me an incident and a bad version, e.g. `INC-1 bad=v2.14.0`.")
            return
        bad = m.group(1)
        target = rollback_target(await self.svc.deploys(), bad)
        if not target:
            await agent.say(ev.room_id, f"⚠️ {inc.id}: no earlier good release than `{bad}`. A human has to take this one.")
            return
        inc.bad_version, inc.target_version, inc.status = bad, target["version"], "awaiting_approval"
        approvers = " ".join(f"@{a}" for a in self.s.approvers) or "anyone on call"
        await agent.say(
            ev.room_id,
            f"🔧 **Rollback plan for {inc.id}**\n"
            f"• `{self.s.service_name}`: `{bad}` → `{target['version']}` "
            f"(`{target.get('sha')}`, \"{target.get('message')}\")\n"
            f"• blast radius: one service, stateless, ~10s\n"
            f"• verify: error rate < {self.s.error_rate_threshold:.0%} for 30s\n"
            f"**Needs human sign-off** ({approvers}). Reply `@{self.name('fixer')} approve {inc.id}` "
            f"or `@{self.name('fixer')} reject {inc.id}`.",
        )

    async def _execute_rollback(self, agent: SwitchAgent, inc: Incident, approver: str) -> None:
        inc.status = "mitigating"
        inc.notes.append(f"rollback approved by {approver}")
        await agent.say(inc.room_id, f"⏪ Approved by {approver}. Rolling `{self.s.service_name}` back to `{inc.target_version}`…")
        try:
            await self.svc.deploy(inc.target_version, actor=f"{self.name('fixer')} (approved by {approver})")
        except httpx.HTTPError as exc:
            inc.status = "awaiting_approval"
            await agent.say(inc.room_id, f"❌ Deploy call failed: `{exc}`. Nothing changed. I'll retry on the next approval.")
            return
        for _ in range(12):
            await asyncio.sleep(2.5)
            h = await self.svc.health()
            if h["requests_30s"] >= 10 and h["error_rate"] < self.s.error_rate_threshold:
                await agent.say(inc.room_id, f"🟢 `{inc.target_version}` is live and healthy: error rate "
                                f"{h['error_rate']*100:.0f}% over {h['requests_30s']} requests.")
                return
        await agent.say(inc.room_id, f"⚠️ Rolled back but {inc.id} isn't recovering. Escalating to humans: "
                        f"this probably isn't (only) `{inc.bad_version}`.")

    # -------------------------------------------------------------------- scribe
    async def on_scribe(self, agent: SwitchAgent, ev: RoomEvent) -> None:
        if ev.type != "message":
            return
        inc_id = incident_id(ev.body)
        inc = self.incidents.get(inc_id) if inc_id else None
        if not inc:
            done = [i for i in self.incidents.values() if i.status == "resolved"]
            inc = done[-1] if done else None
        if not inc:
            await agent.say(ev.room_id, "📝 No closed incident to write up yet.")
            return
        await agent.connect_room(ev.room_id)
        history = await agent.history(ev.room_id, limit=200)
        since = (inc.opened_at - 60) * 1000
        lines = [
            f"[{ts((m.get('timestamp') or 0) / 1000)}] {m.get('sender_name') or m.get('sender')}: {m.get('body')}"
            for m in history
            if (m.get("timestamp") or 0) >= since and m.get("kind", "message") == "message"
        ]
        timeline = defang("\n".join(lines[-80:]) or "\n".join(inc.notes))
        ttr = ((inc.resolved_at or time.time()) - inc.opened_at) / 60
        fallback = (
            f"# Postmortem: {inc.id}, {self.s.service_name}\n\n"
            f"**Status:** resolved · **Time to recover:** {ttr:.1f} min · **Blameless:** yes\n\n"
            f"## Summary\n`{inc.bad_version}` caused checkout errors. It was rolled back to `{inc.target_version}` "
            f"after human approval.\n\n## Timeline (from the room)\n```\n{timeline}\n```\n\n"
            f"## Decisions\n" + "".join(f"- {n}\n" for n in inc.notes)
            + "\n## Action items\n- [ ] Add a regression test for the failing path\n"
            "- [ ] Canary multi-currency changes behind a flag\n- [ ] Alert on per-currency error rate\n"
        )
        doc = await brain.write(
            "You are snitch-scribe. Write a concise, blameless incident postmortem in markdown with "
            "sections: Summary, Impact, Timeline, Root cause, What went well, Action items (checkboxes, "
            "with owners where the room names them). Use only facts from the room transcript.",
            f"Incident {inc.id} on {self.s.service_name}. TTR {ttr:.1f} min. Bad: {inc.bad_version}. "
            f"Rolled back to {inc.target_version}. Notes: {inc.notes}\n\nRoom transcript:\n{timeline}",
            fallback, self.s.anthropic_model, max_tokens=1500,
        )
        name = f"postmortem-{inc.id.lower()}"
        try:
            await agent.op("create_room_document", name=name,
                           description=f"Blameless postmortem for {inc.id}",
                           instructions="Read before changing checkout-service.", content=doc)
            where = f"saved as room document **`{name}`**, so every agent and human here can load it"
        except RuntimeError as exc:
            log.warning("room document failed: %s", exc)
            where = "couldn't save it as a room document, so it's posted inline"
        await agent.say(ev.room_id, f"📝 Postmortem for {inc.id} {where}.\n\n{defang(doc[:3500])}")
