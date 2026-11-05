"""checkout-service: a small, real HTTP service that SNITCH guards.

It keeps a deploy ledger, serves traffic, logs errors, and can be redeployed to
any known version over HTTP. Version v2.14.0 ships a real bug (a currency
lookup that blows up on a missing key), so deploying it really breaks
/checkout, and rolling back really fixes it.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

RELEASES: dict[str, dict[str, str]] = {
    "v2.13.1": {"sha": "4be19c2", "author": "alex", "message": "Cache tax tables per region"},
    "v2.13.2": {"sha": "9f02d1a", "author": "sam", "message": "Retry payment provider on 502"},
    "v2.14.0": {"sha": "c0ffee7", "author": "priya", "message": "Multi-currency checkout (EUR, GBP, JPY)"},
}
BROKEN = {"v2.14.0"}
FX = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 151.2}
FX_V214 = {"USD": 1.0, "EUR": 0.92}  # v2.14.0's new FX table forgot GBP and JPY


class DeployRequest(BaseModel):
    version: str
    actor: str = "ci"


class State:
    def __init__(self) -> None:
        self.version = "v2.13.2"
        self.deploys: list[dict[str, Any]] = []
        self.requests: deque[tuple[float, bool]] = deque(maxlen=2000)
        self.logs: deque[dict[str, Any]] = deque(maxlen=500)
        self.record_deploy("v2.13.1", "ci", at=time.time() - 86400)
        self.record_deploy("v2.13.2", "ci", at=time.time() - 3600 * 5)

    def record_deploy(self, version: str, actor: str, at: float | None = None) -> dict[str, Any]:
        rel = RELEASES[version]
        entry = {"version": version, "actor": actor, "at": at or time.time(), **rel}
        self.deploys.append(entry)
        self.version = version
        return entry

    def error_rate(self, window: float = 30.0) -> tuple[float, int]:
        cutoff = time.time() - window
        recent = [ok for t, ok in self.requests if t >= cutoff]
        if not recent:
            return 0.0, 0
        return 1 - (sum(recent) / len(recent)), len(recent)


def checkout(version: str, currency: str, amount: float) -> float:
    if version in BROKEN:
        return round(amount * FX_V214[currency], 2)  # KeyError on GBP/JPY: the bug
    return round(amount * FX.get(currency, 1.0), 2)


def create_app(traffic: bool = True) -> FastAPI:
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="checkout-service")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    state = State()
    app.state.svc = state

    def handle(currency: str, amount: float) -> dict[str, Any]:
        try:
            total = checkout(state.version, currency, amount)
            state.requests.append((time.time(), True))
            return {"ok": True, "total": total, "version": state.version}
        except Exception as exc:  # noqa: BLE001 - we log and 500 like a real service
            state.requests.append((time.time(), False))
            state.logs.append({
                "at": time.time(), "level": "ERROR", "version": state.version,
                "message": f"POST /checkout 500 {type(exc).__name__}: {exc!r} currency={currency}",
                "trace": 'File "checkout/pricing.py", line 42, in convert\n    return round(amount * FX_V214[currency], 2)',
            })
            raise HTTPException(status_code=500, detail="checkout failed") from exc

    @app.post("/checkout")
    def post_checkout(currency: str = "USD", amount: float = 10.0) -> dict[str, Any]:
        return handle(currency, amount)

    @app.get("/health")
    def health() -> dict[str, Any]:
        rate, n = state.error_rate()
        return {
            "service": "checkout-service", "version": state.version,
            "error_rate": round(rate, 3), "requests_30s": n,
            "status": "degraded" if rate > 0.1 else "ok",
        }

    @app.get("/deploys")
    def deploys() -> list[dict[str, Any]]:
        return state.deploys

    @app.get("/releases")
    def releases() -> dict[str, Any]:
        return RELEASES

    @app.get("/logs")
    def logs(level: str = "ERROR", limit: int = 50) -> list[dict[str, Any]]:
        return [l for l in state.logs if l["level"] == level][-limit:]

    @app.post("/deploy")
    def deploy(req: DeployRequest) -> dict[str, Any]:
        if req.version not in RELEASES:
            raise HTTPException(status_code=404, detail=f"unknown version {req.version}")
        state.requests.clear()
        return state.record_deploy(req.version, req.actor)

    async def synthetic_traffic() -> None:
        currencies = ["USD", "USD", "EUR", "GBP", "JPY"]
        while True:
            try:
                handle(random.choice(currencies), round(random.uniform(5, 200), 2))
            except HTTPException:
                pass
            await asyncio.sleep(0.2)

    if traffic:
        app.router.on_startup.append(lambda: setattr(app.state, "traffic", asyncio.create_task(synthetic_traffic())))

    return app
