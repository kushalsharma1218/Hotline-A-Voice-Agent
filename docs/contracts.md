# Hotline — build contracts (tech lead, binding for all workstreams)

Source of truth: `VoiceHub Lead Desk — PRD.pdf` in repo root. This file pins the
cross-team interfaces so workstreams can build in parallel. If the PRD and this
file disagree, this file wins; if you need to change a contract, say so in your
final report instead of silently diverging.

## Repo layout (who owns what)

| Path | Owner workstream |
|---|---|
| `docker-compose.yml`, `.env.example`, `.gitignore`, `n8n/Dockerfile`, `db/*.sql` | INFRA |
| `ingress/**` | INGRESS |
| `prompts/**`, `eval/*.py`, `eval/tests/**`, `eval/results/`, `eval/pyproject.toml` | EVAL |
| `eval/dataset.jsonl`, `eval/seeds.jsonl` | DATASET |
| `n8n/workflows/*.json`, `agent/agent_prompt.md` | N8N |
| `README.md`, `docs/architecture.md` | DOCS (runs last) |

Never edit files owned by another workstream.

## Environment variables (exact names)

`POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB DATABASE_URL N8N_ENCRYPTION_KEY
WEBHOOK_URL N8N_BASE_URL N8N_EDITOR_BASE_URL NODE_FUNCTION_ALLOW_EXTERNAL
ELEVENLABS_WEBHOOK_SECRET ELEVENLABS_AGENT_ID INTERNAL_TOKEN ADMIN_TOKEN
ANTHROPIC_API_KEY EVAL_MODEL SALESFORCE_INSTANCE_URL SLACK_APPROVAL_CHANNEL
SLACK_ALERT_CHANNEL`

- `ELEVENLABS_AGENT_ID` may be a comma-separated allowlist.
- `DATABASE_URL` uses `postgresql://` scheme (psycopg 3 conninfo).
- n8n reads `N8N_EDITOR_BASE_URL`, `SALESFORCE_INSTANCE_URL`, `SLACK_*_CHANNEL`
  via `$env` in expressions (compose must pass them and set
  `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`).

## Services / ports (docker compose network)

- `postgres` : 5432, image `postgres:16-alpine`. One DB (`POSTGRES_DB`, e.g.
  `hotline`); n8n uses schema `public`, app uses schema `hotline`.
- `n8n` : 5678, custom image from `n8n/Dockerfile` (pinned n8n version + `ajv`).
  `/prompts` is `./prompts` mounted read-only. `/healthz` returns 200.
- `ingress` : 8000, built from `ingress/Dockerfile`.

## Ingress → n8n forward

`POST {N8N_BASE_URL}/webhook/hotline/post-call`, header
`X-Hotline-Token: <INTERNAL_TOKEN>`, JSON body:

```json
{"conversation_id":"conv_abc123","agent_id":"agent_xyz",
 "received_at":"2026-09-24T10:15:02Z","call_duration_secs":184,
 "caller_phone":null,"user_turns":9,
 "transcript_text":"Agent: Hi...\nCaller: ..."}
```

Transcript flattening: one line per turn, `Agent: <message>` for role `agent`,
`Caller: <message>` for role `user`, joined with `\n`; turns with empty/null
message are skipped. `user_turns` = count of role `user` turns with non-empty
message.

Any 2xx from n8n = success.

## Database (schema `hotline`)

Exactly the three tables in PRD §6.3 (`call_events`, `extraction_runs`,
`decisions`) with those columns, types and CHECK constraints. No extra tables.

Status values: `RECEIVED FORWARDED FORWARD_FAILED EXTRACTED LOGGED_ONLY
PENDING_APPROVAL WRITTEN REJECTED EXPIRED FAILED`.
Routes: `LOGGED_ONLY AUTO_WRITE APPROVAL`.

Every status change is a guarded update:
`UPDATE hotline.call_events SET status=$new, updated_at=now() WHERE conversation_id=$id AND status=$expected`.

**Amendment 2 (tech lead): replay claims the row first.** n8n acks 202 before
the ingress records FORWARDED, so n8n's first guarded update may run while the
row is still in its pre-forward state. Therefore:
- n8n's first transition accepts `status IN ('RECEIVED','FORWARDED')`.
- Admin replay first does a guarded `FORWARD_FAILED|FAILED -> RECEIVED`
  (0 rows -> 409, so concurrent replays can't both run), then runs the normal
  forward (`RECEIVED -> FORWARDED | FORWARD_FAILED`). Extra state-machine edge:
  `FORWARD_FAILED/FAILED --> RECEIVED: replay`.
- A row stuck in `RECEIVED` for more than 5 minutes (ingress crashed before the
  background forward) is also replayable.
- Because a replayed call runs the n8n workflow again, all n8n writes are
  idempotent: `decisions` is an upsert on conversation_id, Salesforce Lead is an
  upsert on `Conversation_Id__c`, Task is skipped if `decisions.sf_task_id` set.

Failure convention (for "failures by node" monitoring): whenever a call goes to
`FAILED`, `last_error` is written as `"<node name>: <message>"` (truncated to
1000 chars). Ingress forward failures write `"forward: <message>"`.

## Extraction schema (prompts/lead_qualification/vN/schema.json)

JSON Schema draft 2020-12, `"type":"object"`, all properties required,
`additionalProperties:false` on every object.

```
is_sales_lead        boolean
contact              object {first_name, last_name, email, phone, company, job_title}
                     each: ["string","null"]
need_summary         string, maxLength 300
budget               object {mentioned: boolean,
                             amount_usd: ["number","null"] (minimum 0),
                             period: enum monthly|annual|one_time|unknown}
timeline             enum immediate|within_3_months|within_12_months|exploring|unknown
decision_maker       enum yes|no|unknown
buying_intent_score  integer, minimum 1, maximum 10
intent_evidence      string, maxLength 200
follow_up_required   boolean
call_summary         string, maxLength 500
```

`meta.json`: `{"version":"lead_qualification@v1","tool_name":"record_lead"}`
(v2: `lead_qualification@v2`).

## Claude request shape (shared by n8n Code node and eval/request.py)

```json
{
  "model": "<model>",
  "max_tokens": 1024,
  "temperature": 0,
  "system": "<system.md contents>",
  "tools": [{"name": "<meta.tool_name>",
             "description": "Record the qualified lead extracted from the call transcript.",
             "input_schema": <schema.json with top-level "$schema" and "$id" keys removed>}],
  "tool_choice": {"type": "tool", "name": "<meta.tool_name>"},
  "messages": [{"role": "user",
                "content": "<transcript>\n{transcript_text}\n</transcript>"}]
}
```

**Amendment 1 (tech lead, after API check): per-model sampling/thinking params.**
`claude-sonnet-5` rejects `temperature` (400) and runs adaptive thinking by
default, which is incompatible with forced `tool_choice`. So `temperature` is
NOT a fixed key; the builder applies a per-model profile after building the
base request (base request = the shape above minus `temperature`):

```js
const MODEL_PARAMS = {
  "claude-sonnet-5":           { thinking: { type: "disabled" } },
  "claude-haiku-4-5-20251001": { temperature: 0 },
  "claude-haiku-4-5":          { temperature: 0 },
};
// unknown model -> no extra keys
```
Keys are merged in this order: model, max_tokens, [temperature], [thinking],
system, tools, tool_choice, messages. `max_tokens` stays 1024 (PRD cost cap).
A response with `stop_reason == "max_tokens"` or with no `tool_use` block is
treated as invalid output (error path `/`, message
`no complete tool_use block (stop_reason=<reason>)`), which triggers the one
retry like a schema failure. If there is no tool_use block, the retry re-sends
attempt 1 unchanged (no tool_result can be built).

**Amendment 3 (tech lead): validator in n8n is `@cfworker/json-schema`, not Ajv.**
n8n 2.x's Code-node task runner forbids code generation from strings, which
Ajv's compiler needs. We do NOT enable `N8N_RUNNERS_INSECURE_MODE` (it disables
the sandbox for all Code nodes). `@cfworker/json-schema` (Draft 2020-12, no
codegen) is installed in the n8n image and allowed via
`NODE_FUNCTION_ALLOW_EXTERNAL=@cfworker/json-schema`. Construct with
`new Validator(schema, '2020-12', false)` (shortCircuit false = all errors).
The Validate Code node normalizes its output to the eval format: take errors
whose keyword is a leaf keyword (type, enum, const, minimum, maximum,
minLength, maxLength, required, additionalProperties/ false-schema), convert
`instanceLocation` `#/a/b` -> `/a/b` (`#` -> ``), and message text in the
Ajv-style wording used by eval/validate.py (e.g. `must be <= 10`,
`must NOT have additional properties`, `must be string,null`,
`must have required property 'x'`, `must be equal to one of the allowed values`,
`must NOT have more than 300 characters`). Same schema file => same rules.
Everywhere the PRD says "Ajv", read "the n8n JSON Schema validator".

Retry (attempt 2) appends: assistant message = the original response `content`
array; then user message with content
`[{"type":"tool_result","tool_use_id":"<id>","is_error":true,
   "content":"Schema validation failed:\n- <path>: <message>\n..."}]`.

Endpoint `https://api.anthropic.com/v1/messages`, headers `x-api-key`,
`anthropic-version: 2023-06-01`, `content-type: application/json`.

Models: default `claude-sonnet-5`; comparison `claude-haiku-4-5-20251001`.

## Routing (n8n Switch == eval/scoring.py)

```
not is_sales_lead            -> LOGGED_ONLY
buying_intent_score >= 7     -> APPROVAL      (Rating Hot)
else                         -> AUTO_WRITE    (Rating 4-6 Warm, 1-3 Cold)
```
`INTENT_APPROVAL_THRESHOLD = 7`, `MIN_USER_TURNS = 2` (fewer user turns ->
LOGGED_ONLY with no Claude call).

## Eval dataset row (eval/dataset.jsonl)

```json
{"id":"hot_01","category":"hot","adversarial":null,
 "transcript":"Agent: ...\nCaller: ...",
 "expected": { <full schema object, must validate against v1 schema> },
 "expected_route":"APPROVAL"}
```
`category` in `not_lead|cold|warm|hot`; `adversarial` in
`null|contradiction|non_usd_budget|prompt_injection`.
