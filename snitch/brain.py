"""Optional LLM layer. With ANTHROPIC_API_KEY set, agents write with Claude;
without it, they fall back to deterministic templates and still work end to end."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("snitch.brain")

_client = None


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


async def write(system: str, prompt: str, fallback: str, model: str, max_tokens: int = 900) -> str:
    global _client
    if not enabled():
        return fallback
    try:
        if _client is None:
            from anthropic import AsyncAnthropic

            _client = AsyncAnthropic()
        msg = await _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        return text or fallback
    except Exception as exc:  # never let the LLM take the incident down with it
        log.warning("LLM unavailable, using template: %s", exc)
        return fallback
