"""Pure investigation logic: who broke prod, and what do we roll back to.

Kept free of I/O so it is unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class Verdict:
    suspect: dict[str, Any] | None
    previous: dict[str, Any] | None
    first_error_at: float | None
    error_count: int
    confidence: float
    signature: str
    reasons: list[str]


def ts(t: float | None) -> str:
    if t is None:
        return "unknown"
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S UTC")


def error_signature(logs: list[dict[str, Any]]) -> str:
    """The most common error line, with variable parts stripped."""
    counts: dict[str, int] = {}
    for entry in logs:
        msg = str(entry.get("message", ""))
        key = msg.split(" currency=")[0]
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "no errors in logs"
    return max(counts, key=counts.get)


def correlate(deploys: list[dict[str, Any]], logs: list[dict[str, Any]], window: float = 1800) -> Verdict:
    """Pin the error spike on the deploy that most plausibly caused it."""
    ordered = sorted(deploys, key=lambda d: d["at"])
    errors = sorted((l for l in logs if l.get("level", "ERROR") == "ERROR"), key=lambda l: l["at"])
    first_err = errors[0]["at"] if errors else None
    reasons: list[str] = []

    if not ordered:
        return Verdict(None, None, first_err, len(errors), 0.0, error_signature(errors), ["no deploy history"])

    suspect_idx = None
    if first_err is not None:
        for i, d in enumerate(ordered):
            if d["at"] <= first_err:
                suspect_idx = i
    if suspect_idx is None:
        suspect_idx = len(ordered) - 1
        reasons.append("no errors predate a deploy; defaulting to the latest deploy")

    suspect = ordered[suspect_idx]
    previous = ordered[suspect_idx - 1] if suspect_idx > 0 else None
    confidence = 0.4

    if first_err is not None:
        gap = first_err - suspect["at"]
        reasons.append(f"first error {gap:.0f}s after {suspect['version']} went out")
        if gap <= 120:
            confidence += 0.35
        elif gap <= window:
            confidence += 0.15
        else:
            confidence -= 0.2
            reasons.append("gap is large; the deploy may be a coincidence")

    tagged = [l for l in errors if l.get("version")]
    if tagged:
        on_suspect = sum(1 for l in tagged if l["version"] == suspect["version"])
        share = on_suspect / len(tagged)
        reasons.append(f"{on_suspect}/{len(tagged)} errors were served by {suspect['version']}")
        confidence += 0.25 * share
    if previous and previous.get("version") == suspect.get("version"):
        previous = None

    return Verdict(
        suspect=suspect,
        previous=previous,
        first_error_at=first_err,
        error_count=len(errors),
        confidence=max(0.0, min(confidence, 0.99)),
        signature=error_signature(errors),
        reasons=reasons,
    )


def rollback_target(deploys: list[dict[str, Any]], bad_version: str) -> dict[str, Any] | None:
    """The newest deploy before the bad one that isn't the bad version."""
    ordered = sorted(deploys, key=lambda d: d["at"])
    idx = max((i for i, d in enumerate(ordered) if d["version"] == bad_version), default=None)
    if idx is None:
        return None
    for d in reversed(ordered[:idx]):
        if d["version"] != bad_version:
            return d
    return None
