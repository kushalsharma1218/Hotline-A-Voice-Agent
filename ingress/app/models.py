from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

POST_CALL_TRANSCRIPTION = "post_call_transcription"


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class Turn(_Lenient):
    role: str
    message: str | None = None
    time_in_call_secs: float | None = None


class PhoneCall(_Lenient):
    external_number: str | None = None  # the caller for inbound calls


class Metadata(_Lenient):
    call_duration_secs: float | None = None
    phone_call: PhoneCall | None = None


class EventData(_Lenient):
    agent_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    status: str
    transcript: list[Turn] = Field(default_factory=list)
    metadata: Metadata = Field(default_factory=Metadata)


class ElevenLabsEvent(_Lenient):
    type: str
    event_timestamp: int | None = None
    data: EventData


class NormalizedCall(BaseModel):
    """Forward payload to n8n (docs/contracts.md: Ingress -> n8n forward)."""

    conversation_id: str
    agent_id: str
    received_at: datetime
    call_duration_secs: int | None
    caller_phone: str | None
    user_turns: int
    transcript_text: str

    @field_serializer("received_at")
    def _utc_z(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
