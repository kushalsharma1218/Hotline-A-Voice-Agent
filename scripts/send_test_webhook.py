#!/usr/bin/env python3
"""Sign and POST an ElevenLabs `post_call_transcription` webhook to the Hotline ingress.

Stdlib only; runs on Python 3.8+ (e.g. `py scripts/send_test_webhook.py` on Windows).

The signature is built exactly like ElevenLabs does it:
    ElevenLabs-Signature: t=<unix_ts>,v0=<hex HMAC-SHA256(secret, f"{t}.{raw_body}")>

Examples:
    py scripts/send_test_webhook.py                              # 5-turn demo call
    py scripts/send_test_webhook.py --from-dataset hot_01        # transcript from eval/dataset.jsonl
    py scripts/send_test_webhook.py --short                      # 1 caller turn (LOGGED_ONLY, no Claude)
    py scripts/send_test_webhook.py --repeat 3                   # replay: same signed body 3 times
    py scripts/send_test_webhook.py --tamper                     # flip a body byte after signing -> 401
    py scripts/send_test_webhook.py --stale                      # timestamp 2 h old -> 401

Secret and agent id default to ELEVENLABS_WEBHOOK_SECRET / ELEVENLABS_AGENT_ID from the
environment, else from the repo-root .env. Output: one line per send,
`send <i>/<n>: <status> <body>`. Exit code 0 if every request got an HTTP response, 2 on
network errors, 1 on usage errors.
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "http://localhost:8000/webhooks/elevenlabs"

DEMO_TURNS = [
    ("agent", "Hi, this is Maya, an AI assistant for Acme Voice. This call is recorded. "
              "What brings you in today?"),
    ("user", "Hi, I'm Sam Lee from Northwind Logistics. We're looking at voice AI for our "
             "support line."),
    ("agent", "Great. Roughly how many calls a month, and when are you hoping to go live?"),
    ("user", "About twenty thousand calls. We'd like something in the next couple of months."),
    ("agent", "Thanks Sam, someone from our team will follow up by email."),
]
SHORT_TURNS = [
    ("agent", "Hi, this is Maya, an AI assistant for Acme Voice. This call is recorded. "
              "What brings you in today?"),
    ("user", "Sorry, wrong number."),
]


def read_dotenv(path):
    """Minimal KEY=VALUE parser (ignores comments/blank lines, strips matching quotes)."""
    values = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                values[key] = value
    except OSError:
        pass
    return values


def setting(name, dotenv):
    return os.environ.get(name) or dotenv.get(name) or ""


def turns_from_dataset(dataset_path, row_id):
    """Convert a dataset row's `Agent:`/`Caller:` lines into ElevenLabs transcript turns."""
    with open(dataset_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("id") != row_id:
                continue
            turns = []
            for text_line in row["transcript"].split("\n"):
                if text_line.startswith("Agent:"):
                    turns.append(["agent", text_line[len("Agent:"):].strip()])
                elif text_line.startswith("Caller:"):
                    turns.append(["user", text_line[len("Caller:"):].strip()])
                elif turns and text_line.strip():
                    # Continuation of a multi-line turn.
                    turns[-1][1] += "\n" + text_line.strip()
            return [tuple(t) for t in turns]
    raise SystemExit("dataset row %r not found in %s" % (row_id, dataset_path))


def build_event(conversation_id, agent_id, turns, phone):
    transcript = []
    t = 0.0
    for role, message in turns:
        transcript.append({"role": role, "message": message, "time_in_call_secs": round(t, 1)})
        t += 6.5
    now = int(time.time())
    metadata = {"start_time_unix_secs": now - int(t), "call_duration_secs": int(t) + 3}
    if phone:
        metadata["phone_call"] = {"direction": "inbound", "external_number": phone,
                                  "agent_number": "+15550000000", "type": "twilio"}
    return {
        "type": "post_call_transcription",
        "event_timestamp": now,
        "data": {
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "status": "done",
            "transcript": transcript,
            "metadata": metadata,
            "analysis": {"call_successful": "success", "transcript_summary": ""},
        },
    }


def sign(body, secret, timestamp):
    mac = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body,
                   hashlib.sha256).hexdigest()
    return "t=%d,v0=%s" % (timestamp, mac)


def tamper(body):
    """Flip one byte of the first transcript message (keeps the JSON valid) after signing."""
    marker = b'"message": "'
    idx = body.find(marker)
    idx = idx + len(marker) if idx >= 0 else len(body) // 2
    for i in range(idx, len(body)):
        ch = body[i]
        if 0x41 <= ch <= 0x5A or 0x61 <= ch <= 0x7A:  # ASCII letter: toggle case
            return body[:i] + bytes([ch ^ 0x20]) + body[i + 1:]
    return body[:idx] + bytes([body[idx] ^ 0x01]) + body[idx + 1:]


def post(url, body, signature, timeout):
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "ElevenLabs-Signature": signature,
        "User-Agent": "hotline-send-test-webhook/1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main(argv=None):
    dotenv = read_dotenv(os.path.join(REPO_ROOT, ".env"))
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", default=DEFAULT_URL, help="ingress webhook URL (default %(default)s)")
    p.add_argument("--secret", default=None,
                   help="HMAC secret (default: ELEVENLABS_WEBHOOK_SECRET from env or .env)")
    p.add_argument("--agent-id", default=None,
                   help="agent id (default: first of ELEVENLABS_AGENT_ID from env or .env)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--from-dataset", metavar="ID", help="use the transcript of this eval row")
    src.add_argument("--short", action="store_true",
                     help="1 caller turn: below MIN_USER_TURNS, ends LOGGED_ONLY with no Claude call")
    p.add_argument("--dataset", default=os.path.join(REPO_ROOT, "eval", "dataset.jsonl"),
                   help="dataset path for --from-dataset (default %(default)s)")
    p.add_argument("--conversation-id", default=None, help="default: random conv_test_<hex>")
    p.add_argument("--phone", default=None, help="caller number for metadata.phone_call")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="send the identical signed body N times (replay test)")
    p.add_argument("--tamper", action="store_true", help="flip a body byte after signing")
    p.add_argument("--stale", action="store_true", help="sign with a timestamp 2 hours old")
    p.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    args = p.parse_args(argv)

    secret = args.secret if args.secret is not None else setting("ELEVENLABS_WEBHOOK_SECRET", dotenv)
    if not secret:
        p.error("no secret: pass --secret or set ELEVENLABS_WEBHOOK_SECRET (env or .env)")
    agent_id = args.agent_id
    if agent_id is None:
        agent_id = setting("ELEVENLABS_AGENT_ID", dotenv).split(",")[0].strip()
    if not agent_id:
        p.error("no agent id: pass --agent-id or set ELEVENLABS_AGENT_ID (env or .env)")
    if args.repeat < 1:
        p.error("--repeat must be >= 1")

    if args.from_dataset:
        turns = turns_from_dataset(args.dataset, args.from_dataset)
    elif args.short:
        turns = SHORT_TURNS
    else:
        turns = DEMO_TURNS
    cid = args.conversation_id or "conv_test_" + secrets.token_hex(8)

    body = json.dumps(build_event(cid, agent_id, turns, args.phone)).encode("utf-8")
    ts = int(time.time()) - (7200 if args.stale else 0)
    signature = sign(body, secret, ts)
    if args.tamper:
        body = tamper(body)

    print("conversation_id=%s agent_id=%s turns=%d user_turns=%d%s%s" % (
        cid, agent_id, len(turns), sum(1 for r, m in turns if r == "user" and m.strip()),
        " tampered" if args.tamper else "", " stale" if args.stale else ""))
    rc = 0
    for i in range(1, args.repeat + 1):
        try:
            status, text = post(args.url, body, signature, args.timeout)
            print("send %d/%d: %d %s" % (i, args.repeat, status, text.strip()))
        except (urllib.error.URLError, OSError) as exc:
            print("send %d/%d: ERROR %s" % (i, args.repeat, exc))
            rc = 2
        sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
