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


def do_tool(s: Settings, cmd: str, arg: str | None, approved_by: str | None) -> None:
    """Shell tools for Console-run agents (Claude Code sessions in a Switch room)."""
    from .investigate import correlate, rollback_target, ts

    http = httpx.Client(base_url=s.service_url, timeout=10)
    if cmd == "status":
        h = http.get("/health").json()
        print(f"{h['service']} is {h['status'].upper()} on {h['version']}: error rate "
              f"{h['error_rate']*100:.0f}% over {h['requests_30s']} requests (last 30s)")
    elif cmd == "evidence":
        deploys, logs = http.get("/deploys").json(), http.get("/logs?level=ERROR&limit=200").json()
        v = correlate(deploys, logs)
        if not v.suspect:
            sys.exit("No deploy history.")
        sus = v.suspect
        print(f"SUSPECT: {sus['version']} (sha {sus.get('sha')}, author @{sus.get('author')}, "
              f'"{sus.get("message")}") deployed {ts(sus["at"])}')
        print(f"PREVIOUS GOOD: {v.previous['version'] if v.previous else 'none'}")
        print(f"ERRORS: {v.error_count}, first at {ts(v.first_error_at)}: {v.signature}")
        print("EVIDENCE: " + "; ".join(v.reasons))
        print(f"CONFIDENCE: {v.confidence:.0%}")
        if logs:
            print("TRACE:\n" + str(logs[-1].get("trace", "")))
    elif cmd == "plan":
        if not arg:
            sys.exit("usage: snitch plan <bad-version>")
        t = rollback_target(http.get("/deploys").json(), arg)
        if not t:
            sys.exit(f"No earlier release than {arg}; a human must handle this.")
        print(f"ROLLBACK PLAN: {arg} -> {t['version']} (sha {t.get('sha')}, \"{t.get('message')}\")")
        print("This changes production. Do NOT run `snitch rollback` until a human approves in the room.")
    elif cmd == "rollback":
        if not arg or not approved_by:
            sys.exit("usage: snitch rollback <version> --approved-by <human>. Refusing without a named human approver.")
        r = http.post("/deploy", json={"version": arg, "actor": f"snitch-fixer (approved by {approved_by})"})
        r.raise_for_status()
        import time
        for _ in range(12):
            time.sleep(2.5)
            h = http.get("/health").json()
            if h["requests_30s"] >= 10 and h["error_rate"] < s.error_rate_threshold:
                print(f"RECOVERED: {h['version']} healthy, error rate {h['error_rate']*100:.0f}% over {h['requests_30s']} requests")
                return
        print("NOT RECOVERED after 30s: escalate to the humans in the room.")


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
    p.add_argument("command", choices=["register", "import", "status", "evidence", "plan", "rollback", "run", "victim", "break", "mock-switch", "demo", "template"])
    p.add_argument("arg", nargs="?", help="version for plan/rollback")
    p.add_argument("--approved-by", default=None, help="human who approved (rollback)")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--dir", default="agents", help="folder holding the Console-created agent dirs (import)")
    args = p.parse_args(argv)
    _log()
    s = Settings.from_env()

    if args.command in ("status", "evidence", "plan", "rollback"):
        logging.disable(logging.CRITICAL)
        do_tool(s, args.command, args.arg, args.approved_by)
    elif args.command == "import":
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
