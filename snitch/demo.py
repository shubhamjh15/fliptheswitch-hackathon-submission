"""`snitch demo`: mock Switch + victim + registration + agents in one process."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from . import brain
from .cli import do_register
from .config import Settings
from .mock_switch import create_app as mock_app
from .runtime import run
from .victim import create_app as victim_app

log = logging.getLogger("snitch.demo")


async def _serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server


async def run_demo(s: Settings, port: int = 8000) -> None:
    s.switch_url = f"http://127.0.0.1:{port}"
    s.service_url = "http://127.0.0.1:9000"
    s.registration_token = s.registration_token or "snitch-local-token"
    s.room_id = ""
    s.state_dir = s.state_dir / "demo"
    if s.state_dir.exists():
        for f in s.state_dir.glob("*.json"):
            f.unlink()
    await _serve(mock_app(s.registration_token), port)
    await _serve(victim_app(), 9000)
    await do_register(s)
    print(f"\n  War room:  http://localhost:{port}      (open this)")
    print("  Service:   http://localhost:9000/health")
    print(f"  LLM:       {'Claude (' + s.anthropic_model + ')' if brain.enabled() else 'templates (set ANTHROPIC_API_KEY for Claude)'}")
    print("  Break it:  click 'Ship v2.14.0' in the room, or `snitch break`\n")
    await run(s)
