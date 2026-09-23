import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.forwarder import Forwarder
from app.main import create_app
from app.models import NormalizedCall
from app.store import Statuses

SECRET = "wsec_test_secret"
AGENT = "agent_test"
ADMIN = "admin-token-123"
N8N = "http://n8n.test"
N8N_URL = f"{N8N}/webhook/hotline/post-call"


class FakeStore:
    """In-memory Store with the same semantics as PgStore (PK dedupe, guarded updates)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def insert_event(self, call: NormalizedCall, raw_payload: dict[str, Any]) -> bool:
        if call.conversation_id in self.rows:
            return False
        self.rows[call.conversation_id] = {
            "conversation_id": call.conversation_id, "agent_id": call.agent_id,
            "status": "RECEIVED", "transcript_text": call.transcript_text,
            "user_turns": call.user_turns, "call_duration_s": call.call_duration_secs,
            "caller_phone": call.caller_phone, "raw_payload": raw_payload,
            "forward_attempts": 0, "last_error": None, "received_at": call.received_at,
            "updated_at": datetime.now(UTC)}
        return True

    async def mark_status(self, cid: str, expected: Statuses, new: str,
                          last_error: str | None = None, inc_attempts: int = 0) -> bool:
        allowed = (expected,) if isinstance(expected, str) else expected
        row = self.rows.get(cid)
        if row is None or row["status"] not in allowed:
            return False
        row["status"] = new
        row["updated_at"] = datetime.now(UTC)
        row["forward_attempts"] += inc_attempts
        if last_error:
            row["last_error"] = last_error[:1000]
        return True

    async def get_event(self, cid: str) -> dict[str, Any] | None:
        return self.rows.get(cid)

    async def claim_for_replay(self, cid: str, stale_after_s: int = 300) -> bool:
        row = self.rows.get(cid)
        if row is None:
            return False
        now = datetime.now(UTC)
        stale = row["status"] == "RECEIVED" and row["updated_at"] < now - timedelta(
            seconds=stale_after_s)
        if row["status"] not in ("FORWARD_FAILED", "FAILED") and not stale:
            return False
        row["status"], row["updated_at"] = "RECEIVED", now
        return True

    async def ping(self) -> None:
        return None


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def sign(body: bytes, ts: int | None = None, secret: str = SECRET) -> str:
    t = str(int(time.time()) if ts is None else ts)
    digest = hmac.new(secret.encode(), t.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={t},v0={digest}"


def event(cid: str = "conv_1", agent: str = AGENT, status: str = "done",
          type_: str = "post_call_transcription", phone: str | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"start_time_unix_secs": 1, "call_duration_secs": 184}
    if phone:
        metadata["phone_call"] = {"type": "twilio", "external_number": phone,
                                  "agent_number": "+15550000000"}
    return {"type": type_, "event_timestamp": 1758000000, "data": {
        "agent_id": agent, "conversation_id": cid, "status": status,
        "transcript": [
            {"role": "agent", "message": "Hi, this is Maya.", "time_in_call_secs": 0},
            {"role": "user", "message": "We need a voice bot.", "time_in_call_secs": 3},
            {"role": "agent", "message": None, "time_in_call_secs": 5, "tool_calls": []},
            {"role": "user", "message": "Budget is 2k a month.", "time_in_call_secs": 9}],
        "metadata": metadata, "analysis": {"call_successful": "success"}}}


def post(client: TestClient, payload: Any, *, ts: int | None = None,
         tamper: bool = False, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    header = sign(body, ts)
    if tamper:
        body = body.replace(b"2k", b"9k")
    return client.post("/webhooks/elevenlabs", content=body,
                       headers={"ElevenLabs-Signature": header,
                                "Content-Type": "application/json"})


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def client(store: FakeStore, sleeps: Sleeps) -> TestClient:
    settings = Settings(_env_file=None, database_url="postgresql://unused", n8n_base_url=N8N,
                        elevenlabs_webhook_secret=SECRET, elevenlabs_agent_id=f"other, {AGENT}",
                        internal_token="internal-tok", admin_token=ADMIN)
    forwarder = Forwarder(store, N8N, "internal-tok", sleep=sleeps)
    with TestClient(create_app(settings, store, forwarder)) as c:
        yield c
