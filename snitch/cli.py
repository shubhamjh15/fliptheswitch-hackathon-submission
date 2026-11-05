"""snitch CLI.

  snitch register          register the four agents with a Switch server (needs a registration token)
  snitch import            use agents you created in Switch Console (reads .switch/agents/*.json)
  snitch run               run the agents + sentinel + alert webhook
  snitch victim            run the demo checkout-service on :9000
  snitch break             ship the buggy release (v2.14.0) to the victim
  snitch mock-switch       run a local Switch-protocol stand-in with a web chat UI
  snitch demo              mock-switch + victim + register + run, all in one
  snitch template          print the Switch room template
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from importlib import resources

import httpx
import uvicorn

from .config import AGENT_DESCRIPTIONS, AGENT_NAMES, Settings
from .switch_client import register_agent


def _log() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-16s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def do_register(s: Settings) -> None:
    if not s.registration_token:
        sys.exit("Set SWITCH_REGISTRATION_TOKEN (Console → Settings, or AGENT_REGISTRATION_TOKEN in Switch's .env).")
    creds = {}
    for role, name in AGENT_NAMES.items():
        creds[role] = await register_agent(s.switch_url, s.registration_token, name,
                                           AGENT_DESCRIPTIONS[role], display_name=name.replace("snitch-", "Snitch ").title())
        print(f"  registered {name:18} id={creds[role]['id']}")
    s.save_credentials(creds)
    print(f"Saved API keys to {s.credentials_path} (shown once by Switch, keep this file private).")
    print("Next: add the four agents to a room (see templates/snitch-warroom.template.yaml) and `snitch run`.")


def do_import(s: Settings, root: str) -> None:
    from .config import import_console_credentials

    creds = import_console_credentials(root)
    missing = [r for r in AGENT_NAMES if r not in creds]
    if missing:
        sys.exit("Missing agents in " + root + ": " + ", ".join(AGENT_NAMES[r] for r in missing)
                 + "\nCreate them in Switch Console (Your Agents -> +) with their folder under that directory.")
    endpoints = {c["endpoint"] for c in creds.values() if c["endpoint"]}
    s.save_credentials(creds)
    print(f"Imported {len(creds)} agents -> {s.credentials_path}")
    if endpoints:
        print("Bridge endpoint(s) Console gave them:", ", ".join(sorted(endpoints)))
        print("Set SWITCH_URL to that (no trailing path) if it differs from your .env.")


async def do_break(s: Settings) -> None:
    async with httpx.AsyncClient(base_url=s.service_url) as http:
        r = await http.post("/deploy", json={"version": "v2.14.0", "actor": "priya (via CI)"})
        r.raise_for_status()
        print(f"Shipped v2.14.0 to {s.service_url}. Grab popcorn.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="snitch", description="Incident war-room agents for Switch.")
    p.add_argument("command", choices=["register", "import", "run", "victim", "break", "mock-switch", "demo", "template"])
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--dir", default="agents", help="folder holding the Console-created agent dirs (import)")
    args = p.parse_args(argv)
    _log()
    s = Settings.from_env()

    if args.command == "import":
        do_import(s, args.dir)
    elif args.command == "register":
        asyncio.run(do_register(s))
    elif args.command == "run":
        from .runtime import run
        asyncio.run(run(s))
    elif args.command == "victim":
        from .victim import create_app
        uvicorn.run(create_app(), host="0.0.0.0", port=args.port or 9000, log_level="warning")
    elif args.command == "break":
        asyncio.run(do_break(s))
    elif args.command == "mock-switch":
        from .mock_switch import create_app as mock
        uvicorn.run(mock(), host="0.0.0.0", port=args.port or 8000, log_level="warning")
    elif args.command == "demo":
        from .demo import run_demo
        asyncio.run(run_demo(s, args.port or 8000))
    elif args.command == "template":
        print(resources.files("snitch").joinpath("templates/snitch-warroom.template.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
