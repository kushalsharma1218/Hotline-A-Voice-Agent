import asyncio
from collections.abc import Awaitable, Callable, Sequence

import httpx

from app.config import log
from app.models import NormalizedCall
from app.store import Store

Sleep = Callable[[float], Awaitable[None]]


class Forwarder:
    """POSTs a NormalizedCall to n8n with retries, then records the outcome as a guarded update."""

    def __init__(self, store: Store, n8n_base_url: str, internal_token: str,
                 sleep: Sleep = asyncio.sleep, backoff_s: Sequence[float] = (1, 4, 16),
                 attempts: int = 3, timeout_s: float = 10.0) -> None:
        self.store = store
        self.url = n8n_base_url.rstrip("/") + "/webhook/hotline/post-call"
        self.token = internal_token
        self.sleep = sleep
        self.backoff_s = backoff_s
        self.attempts = attempts
        self.timeout_s = timeout_s

    async def _post_with_retries(self, call: NormalizedCall) -> tuple[bool, int, str]:
        error = ""
        body = call.model_dump(mode="json")
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            for attempt in range(1, self.attempts + 1):
                try:
                    resp = await client.post(
                        self.url, json=body, headers={"X-Hotline-Token": self.token})
                    if resp.is_success:
                        return True, attempt, ""
                    error = f"n8n returned HTTP {resp.status_code}"
                except httpx.HTTPError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                log("forward_attempt_failed", conversation_id=call.conversation_id,
                    attempt=attempt, error=error)
                if attempt < self.attempts:
                    await self.sleep(self.backoff_s[min(attempt - 1, len(self.backoff_s) - 1)])
        return False, self.attempts, error

    async def forward(self, call: NormalizedCall) -> bool:
        """Returns True if n8n accepted the call. Status: RECEIVED -> FORWARDED | FORWARD_FAILED."""
        ok, attempts, error = await self._post_with_retries(call)
        cid = call.conversation_id
        if ok:
            moved = await self.store.mark_status(cid, "RECEIVED", "FORWARDED", inc_attempts=attempts)
            log("forwarded", conversation_id=cid, attempts=attempts, status_updated=moved)
        else:
            moved = await self.store.mark_status(
                cid, "RECEIVED", "FORWARD_FAILED", last_error=f"forward: {error}", inc_attempts=attempts)
            log("forward_failed", conversation_id=cid, attempts=attempts, error=error,
                status_updated=moved)
        return ok
