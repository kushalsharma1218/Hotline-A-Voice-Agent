from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.models import NormalizedCall

Statuses = str | tuple[str, ...]


class Store(Protocol):
    async def insert_event(self, call: NormalizedCall, raw_payload: dict[str, Any]) -> bool: ...
    async def mark_status(self, cid: str, expected: Statuses, new: str,
                          last_error: str | None = None, inc_attempts: int = 0) -> bool: ...
    async def get_event(self, cid: str) -> dict[str, Any] | None: ...
    async def claim_for_replay(self, cid: str, stale_after_s: int = 300) -> bool: ...
    async def ping(self) -> None: ...


_INSERT = """
INSERT INTO hotline.call_events
  (conversation_id, agent_id, status, transcript_text, user_turns,
   call_duration_s, caller_phone, raw_payload, received_at)
VALUES (%s, %s, 'RECEIVED', %s, %s, %s, %s, %s, %s)
ON CONFLICT (conversation_id) DO NOTHING
RETURNING conversation_id
"""

_MARK = """
UPDATE hotline.call_events
   SET status = %s, updated_at = now(),
       forward_attempts = forward_attempts + %s,
       last_error = COALESCE(%s, last_error)
 WHERE conversation_id = %s AND status = ANY(%s)
"""

# Replay claim (contracts Amendment 2): FORWARD_FAILED|FAILED -> RECEIVED, or a RECEIVED row
# untouched for stale_after_s (ingress died before forwarding). Bumping updated_at makes a
# second concurrent claim on the same row match 0 rows.
_CLAIM = """
UPDATE hotline.call_events
   SET status = 'RECEIVED', updated_at = now()
 WHERE conversation_id = %s
   AND (status IN ('FORWARD_FAILED', 'FAILED')
        OR (status = 'RECEIVED' AND updated_at < now() - make_interval(secs => %s)))
"""

_GET = """
SELECT conversation_id, agent_id, status, transcript_text, user_turns, call_duration_s,
       caller_phone, forward_attempts, last_error, received_at, updated_at
  FROM hotline.call_events WHERE conversation_id = %s
"""


class PgStore:
    """Plain-SQL store over a psycopg 3 async pool. Every status change is a guarded update."""

    def __init__(self, dsn: str) -> None:
        self.pool = AsyncConnectionPool(dsn, min_size=1, max_size=5, open=False)

    async def open(self) -> None:
        await self.pool.open()

    async def close(self) -> None:
        await self.pool.close()

    async def insert_event(self, call: NormalizedCall, raw_payload: dict[str, Any]) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(_INSERT, (
                call.conversation_id, call.agent_id, call.transcript_text, call.user_turns,
                call.call_duration_secs, call.caller_phone, Jsonb(raw_payload), call.received_at))
            return await cur.fetchone() is not None

    async def mark_status(self, cid: str, expected: Statuses, new: str,
                          last_error: str | None = None, inc_attempts: int = 0) -> bool:
        allowed = [expected] if isinstance(expected, str) else list(expected)
        err = last_error[:1000] if last_error else None
        async with self.pool.connection() as conn:
            cur = await conn.execute(_MARK, (new, inc_attempts, err, cid, allowed))
            return cur.rowcount == 1

    async def get_event(self, cid: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(_GET, (cid,))
            return await cur.fetchone()

    async def claim_for_replay(self, cid: str, stale_after_s: int = 300) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(_CLAIM, (cid, stale_after_s))
            return cur.rowcount == 1

    async def ping(self) -> None:
        async with self.pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")
