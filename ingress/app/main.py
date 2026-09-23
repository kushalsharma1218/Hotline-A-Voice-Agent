import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import Settings, log
from app.forwarder import Forwarder
from app.models import POST_CALL_TRANSCRIPTION, ElevenLabsEvent, NormalizedCall
from app.security import InvalidSignature, verify_elevenlabs_signature
from app.store import PgStore, Store
from app.transcript import flatten, user_turns

STALE_RECEIVED_S = 300  # a RECEIVED row untouched this long never got its background forward


def _reply(code: int, status: str, **extra: object) -> JSONResponse:
    return JSONResponse({"status": status, **extra}, status_code=code)


def create_app(settings: Settings | None = None, store: Store | None = None,
               forwarder: Forwarder | None = None) -> FastAPI:
    cfg = settings or Settings()
    pg = PgStore(cfg.database_url) if store is None else None
    db: Store = store if store is not None else pg  # type: ignore[assignment]
    fwd = forwarder or Forwarder(db, cfg.n8n_base_url, cfg.internal_token)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if missing := cfg.missing():
            raise RuntimeError(f"missing required env: {', '.join(missing)}")
        if pg is not None:
            await pg.open()
        log("startup", agents=len(cfg.agent_ids))
        yield
        if pg is not None:
            await pg.close()

    app = FastAPI(title="Hotline ingress", lifespan=lifespan)

    @app.post("/webhooks/elevenlabs")
    async def elevenlabs_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
        raw = await request.body()  # raw bytes first: the HMAC covers them exactly
        try:
            verify_elevenlabs_signature(
                raw, request.headers.get("elevenlabs-signature", ""), cfg.elevenlabs_webhook_secret)
        except InvalidSignature as exc:
            log("signature_rejected", logging.WARNING, reason=str(exc))
            return _reply(401, "invalid_signature")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("body is not a JSON object")
            if payload.get("type") != POST_CALL_TRANSCRIPTION:
                log("ignored", reason="event_type", type=str(payload.get("type")))
                return _reply(200, "ignored")
            event = ElevenLabsEvent.model_validate(payload)
        except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
            log("malformed_body", logging.WARNING, error=type(exc).__name__)
            return _reply(400, "malformed")

        data = event.data
        cid = data.conversation_id
        if data.agent_id not in cfg.agent_ids:
            log("ignored", reason="agent_not_allowed", conversation_id=cid, agent_id=data.agent_id)
            return _reply(200, "ignored")
        if data.status != "done":
            log("ignored", reason="call_status", conversation_id=cid, call_status=data.status)
            return _reply(200, "ignored")

        duration = data.metadata.call_duration_secs
        phone = data.metadata.phone_call
        call = NormalizedCall(
            conversation_id=cid, agent_id=data.agent_id,
            received_at=datetime.now(UTC).replace(microsecond=0),
            call_duration_secs=round(duration) if duration is not None else None,
            caller_phone=phone.external_number if phone else None,
            user_turns=user_turns(data.transcript), transcript_text=flatten(data.transcript))
        if not await db.insert_event(call, payload):
            log("duplicate", conversation_id=cid)
            return _reply(200, "duplicate")
        log("accepted", conversation_id=cid, user_turns=call.user_turns)
        background.add_task(fwd.forward, call)
        return _reply(200, "accepted")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        try:
            await db.ping()
        except Exception as exc:  # noqa: BLE001 - any DB failure means unhealthy
            log("healthz_db_down", logging.WARNING, error=type(exc).__name__)
            return _reply(503, "degraded", db="down")
        return _reply(200, "ok", db="ok")

    @app.post("/admin/replay/{conversation_id}")
    async def replay(conversation_id: str, authorization: str = Header(default="")) -> JSONResponse:
        scheme, _, token = authorization.partition(" ")
        if not (cfg.admin_token and scheme.lower() == "bearer"
                and hmac.compare_digest(token.strip().encode(), cfg.admin_token.encode())):
            log("replay_unauthorized", logging.WARNING, conversation_id=conversation_id)
            return _reply(401, "unauthorized")
        row = await db.get_event(conversation_id)
        if row is None:
            return _reply(404, "not_found")
        # Claim first (FORWARD_FAILED|FAILED|stale RECEIVED -> RECEIVED) so n8n's first guarded
        # update (RECEIVED|FORWARDED) matches, and concurrent replays can't both run.
        if not await db.claim_for_replay(conversation_id, STALE_RECEIVED_S):
            return _reply(409, "not_replayable", current=row["status"])
        call = NormalizedCall(
            conversation_id=row["conversation_id"], agent_id=row["agent_id"],
            received_at=row["received_at"], call_duration_secs=row["call_duration_s"],
            caller_phone=row["caller_phone"], user_turns=row["user_turns"],
            transcript_text=row["transcript_text"])
        log("replay", conversation_id=conversation_id, from_status=row["status"])
        if await fwd.forward(call):
            return _reply(200, "FORWARDED")
        return _reply(502, "FORWARD_FAILED")

    return app


app = create_app()
