import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from tests.conftest import ADMIN, AGENT, N8N_URL, event, post


@pytest.fixture
def n8n():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        yield mock.post(N8N_URL)


AUTH = {"Authorization": f"Bearer {ADMIN}"}


def test_valid_signature_accepted_and_forwarded(client, store, n8n):
    n8n.mock(return_value=httpx.Response(200))
    r = post(client, event(phone="+15551234567"))
    assert r.status_code == 200 and r.json() == {"status": "accepted"}

    row = store.rows["conv_1"]
    assert row["status"] == "FORWARDED" and row["forward_attempts"] == 1
    sent = n8n.calls.last.request
    assert sent.headers["X-Hotline-Token"] == "internal-tok"
    body = json.loads(sent.content)
    assert body == {
        "conversation_id": "conv_1", "agent_id": AGENT, "received_at": body["received_at"],
        "call_duration_secs": 184, "caller_phone": "+15551234567", "user_turns": 2,
        "transcript_text": "Agent: Hi, this is Maya.\nCaller: We need a voice bot.\n"
                           "Caller: Budget is 2k a month."}
    assert body["received_at"].endswith("Z")


def test_tampered_body_rejected_nothing_stored(client, store, n8n):
    r = post(client, event(), tamper=True)
    assert r.status_code == 401
    assert store.rows == {} and not n8n.called


def test_stale_timestamp_rejected(client, store, n8n):
    r = post(client, event(), ts=int(time.time()) - 1801)
    assert r.status_code == 401
    assert store.rows == {} and not n8n.called


def test_missing_signature_rejected(client, store):
    r = client.post("/webhooks/elevenlabs", content=json.dumps(event()))
    assert r.status_code == 401 and store.rows == {}


def test_wrong_agent_ignored(client, store, n8n):
    r = post(client, event(agent="agent_intruder"))
    assert r.status_code == 200 and r.json() == {"status": "ignored"}
    assert store.rows == {} and not n8n.called


@pytest.mark.parametrize("kwargs", [{"status": "failed"}, {"type_": "post_call_audio"},
                                    {"type_": "call_initiation_failure"}])
def test_non_done_or_other_type_ignored(client, store, n8n, kwargs):
    r = post(client, event(**kwargs))
    assert r.json() == {"status": "ignored"} and store.rows == {}


def test_other_event_type_with_different_shape_ignored(client, store):
    r = post(client, {"type": "post_call_audio", "data": {"full_audio": "AAAA"}})
    assert r.status_code == 200 and r.json() == {"status": "ignored"}


@pytest.mark.parametrize("raw", [b"{not json", b"[1,2]",
                                 json.dumps({"type": "post_call_transcription",
                                             "data": {"agent_id": AGENT}}).encode()])
def test_malformed_body_400(client, store, raw):
    r = post(client, None, raw=raw)
    assert r.status_code == 400 and store.rows == {}


def test_duplicate_delivery(client, store, n8n):
    n8n.mock(return_value=httpx.Response(200))
    assert post(client, event()).json() == {"status": "accepted"}
    r = post(client, event())
    assert r.status_code == 200 and r.json() == {"status": "duplicate"}
    assert n8n.call_count == 1 and len(store.rows) == 1


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ConnectError("refused")])
def test_n8n_down_then_replay(client, store, n8n, sleeps, failure):
    n8n.mock(side_effect=[failure, failure, failure])
    assert post(client, event()).json() == {"status": "accepted"}
    row = store.rows["conv_1"]
    assert row["status"] == "FORWARD_FAILED"
    assert row["forward_attempts"] == 3 and n8n.call_count == 3
    assert row["last_error"].startswith("forward: ")
    assert sleeps.calls == [1, 4]

    n8n.mock(side_effect=None, return_value=httpx.Response(200))
    r = client.post("/admin/replay/conv_1", headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200 and r.json() == {"status": "FORWARDED"}
    assert row["status"] == "FORWARDED" and row["forward_attempts"] == 4


def test_replay_from_failed(client, store, n8n):
    n8n.mock(return_value=httpx.Response(200))
    post(client, event())
    store.rows["conv_1"]["status"] = "FAILED"
    r = client.post("/admin/replay/conv_1", headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200 and store.rows["conv_1"]["status"] == "FORWARDED"


def test_failed_replay_ends_forward_failed(client, store, n8n):
    n8n.mock(return_value=httpx.Response(500))
    post(client, event())
    store.rows["conv_1"]["status"] = "FAILED"
    r = client.post("/admin/replay/conv_1", headers=AUTH)
    assert r.status_code == 502 and r.json() == {"status": "FORWARD_FAILED"}
    assert store.rows["conv_1"]["status"] == "FORWARD_FAILED"
    assert store.rows["conv_1"]["forward_attempts"] == 6


def _stuck_received(client, store, n8n, age_s: int) -> None:
    n8n.mock(return_value=httpx.Response(200))
    post(client, event())
    row = store.rows["conv_1"]
    row["status"] = "RECEIVED"  # as if ingress died before the background forward
    row["updated_at"] = datetime.now(UTC) - timedelta(seconds=age_s)


def test_stale_received_is_replayable(client, store, n8n):
    _stuck_received(client, store, n8n, age_s=301)
    r = client.post("/admin/replay/conv_1", headers=AUTH)
    assert r.status_code == 200 and store.rows["conv_1"]["status"] == "FORWARDED"


def test_fresh_received_is_not_replayable(client, store, n8n):
    _stuck_received(client, store, n8n, age_s=10)
    calls = n8n.call_count
    r = client.post("/admin/replay/conv_1", headers=AUTH)
    assert r.status_code == 409 and r.json()["current"] == "RECEIVED"
    assert n8n.call_count == calls


def test_concurrent_claim_second_gets_409(client, store, n8n):
    n8n.mock(return_value=httpx.Response(503))
    post(client, event())
    # Another replay has just claimed the row (FORWARD_FAILED -> RECEIVED) and is forwarding.
    assert asyncio.run(store.claim_for_replay("conv_1")) is True
    assert asyncio.run(store.claim_for_replay("conv_1")) is False
    calls = n8n.call_count
    r = client.post("/admin/replay/conv_1", headers=AUTH)
    assert r.status_code == 409 and n8n.call_count == calls


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": ADMIN}, {"Authorization": "Basic x"}])
def test_replay_auth_failure(client, store, n8n, headers):
    n8n.mock(return_value=httpx.Response(503))
    post(client, event())
    calls = n8n.call_count
    r = client.post("/admin/replay/conv_1", headers=headers)
    assert r.status_code == 401
    assert store.rows["conv_1"]["status"] == "FORWARD_FAILED" and n8n.call_count == calls


def test_replay_unknown_and_wrong_state(client, n8n):
    auth = {"Authorization": f"Bearer {ADMIN}"}
    assert client.post("/admin/replay/nope", headers=auth).status_code == 404
    n8n.mock(return_value=httpx.Response(200))
    post(client, event())
    r = client.post("/admin/replay/conv_1", headers=auth)
    assert r.status_code == 409 and r.json()["current"] == "FORWARDED"


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "db": "ok"}
