"""Wire the four agents to Switch and start the alert webhook."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import uvicorn
from fastapi import FastAPI, Request

from .agents import Snitch
from .config import AGENT_NAMES, Settings
from .switch_client import SwitchAgent

log = logging.getLogger("snitch")


def alert_app(snitch: Snitch) -> FastAPI:
    """Accepts Alertmanager / Grafana / generic JSON alerts and opens an incident."""
    app = FastAPI(title="snitch-alerts")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "incidents": {k: v.status for k, v in snitch.incidents.items()}}

    @app.post("/alert")
    async def alert(request: Request) -> dict[str, Any]:
        payload = await request.json()
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        if alerts:  # Alertmanager shape
            firing = [a for a in alerts if a.get("status", "firing") == "firing"]
            if not firing:
                return {"ok": True, "ignored": "resolved"}
            labels = firing[0].get("labels", {})
            source = f"alertmanager:{labels.get('alertname', 'alert')}"
        else:
            source = f"webhook:{payload.get('title', 'alert')}"
        health = await snitch.svc.health()
        inc = await snitch.raise_incident(health, source)
        return {"ok": True, "incident": inc.id if inc else None, "deduped": inc is None}

    return app


async def run(settings: Settings) -> None:
    creds = settings.load_credentials()
    agents = {
        role: SwitchAgent(
            base_url=(creds[role].get("endpoint") or settings.switch_url).rstrip("/"),
            agent_id=creds[role]["id"],
            api_key=creds[role]["api_key"],
            name=AGENT_NAMES[role],
            state_dir=settings.state_dir,
        )
        for role in AGENT_NAMES
    }
    snitch = Snitch(settings, agents)
    handlers = {
        "sentinel": snitch.on_sentinel,
        "detective": snitch.on_detective,
        "fixer": snitch.on_fixer,
        "scribe": snitch.on_scribe,
    }
    server = uvicorn.Server(uvicorn.Config(alert_app(snitch), host="0.0.0.0",
                                           port=settings.alert_port, log_level="warning"))
    log.info("SNITCH up: %s agents -> %s, alerts on :%s, guarding %s",
             len(agents), settings.switch_url, settings.alert_port, settings.service_url)
    tasks = [asyncio.create_task(a.run(handlers[r]), name=r) for r, a in agents.items()]
    tasks.append(asyncio.create_task(snitch.sentinel_watch(), name="watch"))
    tasks.append(asyncio.create_task(server.serve(), name="alerts"))
    try:
        await asyncio.gather(*tasks)
    finally:
        for a in agents.values():
            await a.aclose()
