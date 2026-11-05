# 🐀 SNITCH

**Prod is on fire. SNITCH already knows whose commit did it.**

SNITCH is an incident war room built on [Switch](https://github.com/sandbox-quantum/switch). Four AI agents sit in your team's Slack, Discord, Teams, Mattermost or Telegram channel next to your on-call humans. They spot the outage, name the deploy that caused it, propose a rollback, **wait for a human to approve it**, carry it out, confirm recovery and write the postmortem. Each step happens in the channel, where everyone can see it and step in.

> Track: **Best Business Workflow**. *Flip the Switch Hackathon 2026.*

```
🚨 snitch-sentinel   INC-1 opened · checkout-service degraded · error rate 41% on v2.14.0
                     @snitch-detective INC-1: find out who did this.
🐀 snitch-detective  SNITCHING INC-1: it was v2.14.0 (c0ffee7 by @priya, "Multi-currency checkout")
                     • 38 errors, first at 09:42:03: POST /checkout 500 KeyError: 'JPY'
                     • first error 2s after v2.14.0 went out · 38/38 errors served by v2.14.0 · confidence 99%
🔧 snitch-fixer      Rollback plan for INC-1: v2.14.0 → v2.13.2 … Needs human sign-off.
👩 priya             @snitch-fixer approve INC-1
⏪ snitch-fixer      Approved by priya. Rolling back… 🟢 v2.13.2 is live and healthy.
✅ snitch-sentinel   INC-1 resolved · time to recover 1.4 min. @snitch-scribe write the postmortem.
📝 snitch-scribe     Postmortem saved as room document `postmortem-inc-1`.
```

## Why Switch is central

Without Switch, SNITCH doesn't work. There are four separate agents, and **the Switch room is the only way they talk to each other.** No agent calls another directly; they hand work over by `@`-mentioning each other in the room.

| Switch feature | How SNITCH uses it |
|---|---|
| **Agent protocol** (SSE down, HTTP up) | [`switch_client.py`](snitch/switch_client.py) implements it from scratch: a connection id that persists across reconnects, `Last-Event-ID` resume, a 2-second heartbeat that reopens on 404/409, `gap` frames attached to the next event, `evicted` takeover handling, and the `addressed` filter |
| **Rooms as shared state** | The room *is* the incident log. The Scribe calls `read_context` and writes the postmortem from what people actually said |
| **Addressing** | Agents only wake when `@`-mentioned. Handoffs are mentions, so a human can hijack any step (`@snitch-detective INC-1` re-runs the investigation) |
| **Room documents** | The postmortem is saved with `create_room_document`, so every future agent and human in the room can load it |
| **Bridges** | Same code in Slack, Discord, Teams, Mattermost or Telegram |
| **Room templates** | [`snitch-warroom.template.yaml`](snitch/templates/snitch-warroom.template.yaml) provisions the room, all four agents, the instructions and a kickoff |
| **Human in the loop** | The only destructive action (a deploy) runs only after a human in the channel approves it. The approver can be limited to an allowlist |

## The agents

| Agent | Job |
|---|---|
| `snitch-sentinel` | Polls service health and receives Alertmanager/Grafana webhooks on `:9100/alert`. Opens `INC-n`, dedupes repeat alerts and declares recovery. Answers `status` and `page` |
| `snitch-detective` | Pulls the deploy ledger and error logs, matches the error spike to a release and names the version, SHA and author, with evidence and a confidence score. With `ANTHROPIC_API_KEY`, Claude also reads the stack trace for a likely root cause |
| `snitch-fixer` | Picks the last good release, posts a rollback plan, then **waits**. On `approve` from an allowed human it deploys, then checks recovery against the error budget. If the service doesn't recover, it hands the incident back to the humans |
| `snitch-scribe` | Reads the room history and writes a blameless postmortem (summary, timeline, decisions, action items) as a room document |

## Run it in 60 seconds (no Switch server needed)

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[llm]"
snitch demo
```

Open **http://localhost:8000**, click **💥 Ship v2.14.0** and watch. Then click **✅ approve rollback**.

`snitch demo` starts a local stand-in that speaks the Switch agent protocol on the same wire, plus a chat page and the demo `checkout-service`. That service is real: v2.14.0 ships a genuine `KeyError` on JPY checkouts, and rolling back genuinely fixes it.

## Run it on real Switch

1. Install [Switch Console](https://github.com/sandbox-quantum/switch/releases/latest), start the local server and connect your chat app ([guide](https://docs.switchagents.ai/switch-rooms/deploy/messaging-apps/index)).
2. `cp .env.example .env`. Set `SWITCH_URL` (the agent bridge, usually `http://localhost:8000`) and `SWITCH_REGISTRATION_TOKEN`.
3. Register the four agents (this happens once, and the keys go to `.snitch/agents.json`):
   ```bash
   snitch register
   ```
4. Create the war room from the template in Console (**Templates → Import →** [`snitch-warroom.template.yaml`](snitch/templates/snitch-warroom.template.yaml)) and choose your bridge.
5. Start the guarded service and the agents:
   ```bash
   snitch victim &      # or point SNITCH_SERVICE_URL at your own service
   snitch run
   ```
6. `snitch break`. Then in Slack or Discord: `@snitch-fixer approve INC-1`.

With Docker: `docker compose run --rm snitch register && docker compose up -d`.

### Guarding your own service

SNITCH needs four HTTP endpoints. You can expose them directly or put a thin adapter in front of your deploy tool:

| Endpoint | Returns |
|---|---|
| `GET /health` | `{service, version, error_rate, requests_30s, status}` |
| `GET /deploys` | `[{version, sha, author, message, at, actor}]` |
| `GET /logs?level=ERROR` | `[{at, level, version, message, trace}]` |
| `POST /deploy {version, actor}` | the new deploy entry |

## Tests

```bash
pip install -e ".[dev]" && pytest
```

`tests/test_e2e.py` runs a **full incident over real HTTP and SSE**: it breaks prod, waits for the agents to open the incident and name the bad release, checks that an unauthorised approver is refused, has an authorised human approve, then confirms the rollback happened and the postmortem document exists.

## Layout

```
snitch/switch_client.py   Switch agent-protocol client (SSE + heartbeat + ops)
snitch/agents.py          the four agents
snitch/investigate.py     deploy/error correlation (pure, unit-tested)
snitch/runtime.py         wiring + alert webhook
snitch/victim.py          demo checkout-service
snitch/mock_switch.py     local protocol stand-in + web chat (demo/tests only)
snitch/templates/         Switch room template
```

MIT licensed.
