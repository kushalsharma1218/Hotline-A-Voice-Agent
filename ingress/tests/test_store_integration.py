"""Exercises the real SQL. Runs only when DATABASE_URL points at a DB with db/001_init.sql applied."""
import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest

from app.models import NormalizedCall
from app.store import PgStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"),
]


def test_pg_store_roundtrip():
    async def scenario() -> None:
        store = PgStore(os.environ["DATABASE_URL"])
        await store.open()
        try:
            await store.ping()
            cid = f"it_{uuid.uuid4().hex}"
            call = NormalizedCall(
                conversation_id=cid, agent_id="agent_it",
                received_at=datetime.now(UTC).replace(microsecond=0), call_duration_secs=12,
                caller_phone=None, user_turns=1, transcript_text="Caller: hi")
            assert await store.insert_event(call, {"type": "post_call_transcription"}) is True
            assert await store.insert_event(call, {}) is False
            assert (await store.get_event(cid))["status"] == "RECEIVED"
            assert not await store.mark_status(cid, "FORWARDED", "EXTRACTED")
            assert await store.mark_status(cid, "RECEIVED", "FORWARD_FAILED",
                                           last_error="forward: boom", inc_attempts=3)
            assert await store.claim_for_replay(cid) is True
            assert await store.claim_for_replay(cid) is False  # fresh RECEIVED: not claimable
            assert await store.mark_status(cid, ("RECEIVED", "FAILED"), "FORWARDED",
                                           inc_attempts=1)
            row = await store.get_event(cid)
            assert row["status"] == "FORWARDED" and row["forward_attempts"] == 4
            assert row["last_error"] == "forward: boom"
            async with store.pool.connection() as conn:
                await conn.execute("UPDATE hotline.call_events SET status='RECEIVED', "
                                   "updated_at=now() - interval '6 minutes' "
                                   "WHERE conversation_id=%s", (cid,))
            assert await store.claim_for_replay(cid) is True  # stale RECEIVED
            async with store.pool.connection() as conn:
                await conn.execute("DELETE FROM hotline.call_events WHERE conversation_id=%s",
                                   (cid,))
        finally:
            await store.close()

    asyncio.run(scenario())
