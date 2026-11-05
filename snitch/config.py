"""Runtime configuration, all from environment variables (a .env file is read too)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

AGENT_NAMES = {
    "sentinel": "snitch-sentinel",
    "detective": "snitch-detective",
    "fixer": "snitch-fixer",
    "scribe": "snitch-scribe",
}

AGENT_DESCRIPTIONS = {
    "sentinel": "Watches production. Opens an incident in the room the moment health degrades.",
    "detective": "Correlates errors with deploys and names the commit (and author) that broke prod.",
    "fixer": "Proposes a rollback, waits for a human to approve, executes it and verifies recovery.",
    "scribe": "Reads the room's history and writes a blameless postmortem as a room document.",
}


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    switch_url: str = "http://localhost:8000"
    registration_token: str = ""
    room_id: str = ""
    state_dir: Path = Path(".snitch")
    service_url: str = "http://localhost:9000"
    service_name: str = "checkout-service"
    alert_port: int = 9100
    error_rate_threshold: float = 0.25
    poll_seconds: float = 3.0
    approvers: list[str] = field(default_factory=list)
    anthropic_model: str = "claude-sonnet-5-5"
    github_repo: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        e = os.environ.get
        return cls(
            switch_url=e("SWITCH_URL", cls.switch_url).rstrip("/"),
            registration_token=e("SWITCH_REGISTRATION_TOKEN", ""),
            room_id=e("SNITCH_ROOM_ID", ""),
            state_dir=Path(e("SNITCH_STATE_DIR", ".snitch")),
            service_url=e("SNITCH_SERVICE_URL", cls.service_url).rstrip("/"),
            service_name=e("SNITCH_SERVICE_NAME", cls.service_name),
            alert_port=int(e("SNITCH_ALERT_PORT", str(cls.alert_port))),
            error_rate_threshold=float(e("SNITCH_ERROR_THRESHOLD", str(cls.error_rate_threshold))),
            poll_seconds=float(e("SNITCH_POLL_SECONDS", str(cls.poll_seconds))),
            approvers=[a.strip().lower() for a in e("SNITCH_APPROVERS", "").split(",") if a.strip()],
            anthropic_model=e("SNITCH_MODEL", cls.anthropic_model),
            github_repo=e("SNITCH_GITHUB_REPO", ""),
        )

    @property
    def credentials_path(self) -> Path:
        return self.state_dir / "agents.json"

    def load_credentials(self) -> dict[str, dict]:
        if not self.credentials_path.exists():
            raise SystemExit(
                f"No agent credentials at {self.credentials_path}. Run `snitch register` first."
            )
        return json.loads(self.credentials_path.read_text(encoding="utf-8"))

    def save_credentials(self, creds: dict[str, dict]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.credentials_path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
