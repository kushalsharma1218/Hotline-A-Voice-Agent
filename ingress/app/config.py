import json
import logging
import sys
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Env-driven settings (names per docs/contracts.md). Empty secrets fail closed."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    n8n_base_url: str = "http://n8n:5678"
    elevenlabs_webhook_secret: str = ""
    elevenlabs_agent_id: str = ""  # comma-separated allowlist
    internal_token: str = ""
    admin_token: str = ""

    @property
    def agent_ids(self) -> frozenset[str]:
        return frozenset(a.strip() for a in self.elevenlabs_agent_id.split(",") if a.strip())

    def missing(self) -> list[str]:
        required = ("database_url", "elevenlabs_webhook_secret", "elevenlabs_agent_id",
                    "internal_token", "admin_token")
        return [name.upper() for name in required if not getattr(self, name)]


_logger = logging.getLogger("hotline.ingress")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """One JSON object per line. Callers must never pass secrets or transcripts."""
    _logger.log(level, json.dumps({"event": event, **fields}, default=str))
