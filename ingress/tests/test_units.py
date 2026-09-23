import hashlib
import hmac

import pytest

from app.models import NormalizedCall, Turn
from app.security import InvalidSignature, verify_elevenlabs_signature
from app.transcript import flatten, user_turns

SECRET = "s3cret"
BODY = b'{"type":"post_call_transcription"}'
NOW = 1_758_000_000


def _v0(t: int, body: bytes = BODY) -> str:
    return hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def test_signature_valid():
    verify_elevenlabs_signature(BODY, f"t={NOW},v0={_v0(NOW)}", SECRET, now=NOW)


def test_signature_tolerates_whitespace_and_multiple_v0():
    header = f" t={NOW} , v0={'0' * 64} ,v0={_v0(NOW).upper()} "
    verify_elevenlabs_signature(BODY, header, SECRET, now=NOW)


@pytest.mark.parametrize("header", [
    "", "garbage", f"t={NOW}", f"v0={_v0(NOW)}", f"t=abc,v0={_v0(NOW)}",
    f"t={NOW},v0={'0' * 64}", f"t={NOW},v1={_v0(NOW)}",
])
def test_signature_rejects_malformed_or_wrong(header):
    with pytest.raises(InvalidSignature):
        verify_elevenlabs_signature(BODY, header, SECRET, now=NOW)


@pytest.mark.parametrize("skew", [1801, -1801])
def test_signature_rejects_stale_and_future(skew):
    t = NOW - skew
    with pytest.raises(InvalidSignature):
        verify_elevenlabs_signature(BODY, f"t={t},v0={_v0(t)}", SECRET, now=NOW)


def test_signature_edge_of_window_ok():
    t = NOW - 1800
    verify_elevenlabs_signature(BODY, f"t={t},v0={_v0(t)}", SECRET, now=NOW)


def test_signature_rejects_tampered_body_and_empty_secret():
    with pytest.raises(InvalidSignature):
        verify_elevenlabs_signature(BODY + b" ", f"t={NOW},v0={_v0(NOW)}", SECRET, now=NOW)
    with pytest.raises(InvalidSignature):
        verify_elevenlabs_signature(BODY, f"t={NOW},v0={_v0(NOW)}", "", now=NOW)


TURNS = [
    Turn(role="agent", message="Hi, thanks for calling."),
    Turn(role="user", message="  I need pricing. "),
    Turn(role="agent", message=None),
    Turn(role="user", message=""),
    Turn(role="user", message="   "),
    Turn(role="tool", message="ignored"),
    Turn(role="user", message="Budget is 5k."),
]


def test_flatten():
    assert flatten(TURNS) == (
        "Agent: Hi, thanks for calling.\nCaller: I need pricing.\nCaller: Budget is 5k.")
    assert flatten([]) == ""


def test_user_turns():
    assert user_turns(TURNS) == 2
    assert user_turns([]) == 0


def test_normalized_call_shape():
    from datetime import UTC, datetime
    call = NormalizedCall(conversation_id="c", agent_id="a",
                          received_at=datetime(2026, 9, 24, 10, 15, 2, tzinfo=UTC),
                          call_duration_secs=184, caller_phone=None, user_turns=9,
                          transcript_text="Agent: Hi")
    assert call.model_dump(mode="json") == {
        "conversation_id": "c", "agent_id": "a", "received_at": "2026-09-24T10:15:02Z",
        "call_duration_secs": 184, "caller_phone": None, "user_turns": 9,
        "transcript_text": "Agent: Hi"}
