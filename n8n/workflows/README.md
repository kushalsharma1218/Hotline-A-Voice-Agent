# n8n workflows

These are exported workflows for n8n 2.40.5 (the image pinned in
`n8n/Dockerfile`). They contain no secrets: each node refers to its credential
by name and a placeholder ID.

| File | Workflow ID | What it does |
|---|---|---|
| `wf_post_call.json` | `HtlnPostCall0000` | Receives the ingress forward, calls the extractor, applies the routing policy, writes to Salesforce or asks for approval in Slack, and writes the audit rows (PRD §6.5 steps 1–11) |
| `subwf_claude_extract.json` | `HtlnClaudeExtrct` | Reusable extractor: prompt folder + transcript in, validated JSON out, one retry with error feedback. Contains nothing sales-specific |
| `subwf_error_alert.json` | `HtlnErrorAlert00` | Posts failures to Slack (`#hotline-alerts`) with a link to the execution. This is the error workflow of `wf_post_call` |

The workflow IDs are fixed on purpose. `n8n import:workflow` keeps them, so
three references resolve without editing anything: the Execute Workflow nodes
("Extract" and the three "Alert …" nodes) and `wf_post_call`'s
`settings.errorWorkflow`.

## 1. Environment (already in `docker-compose.yml`)

| Variable | Why |
|---|---|
| `N8N_RESTRICT_FILE_ACCESS_TO=/prompts` | "Load Prompt" reads `/prompts/<dir>/{system.md,schema.json,meta.json}` and `/prompts/model_params.json`. n8n 2.x blocks file access outside this path |
| `NODE_FUNCTION_ALLOW_EXTERNAL=@cfworker/json-schema` | The "Validate" Code node uses it (contracts.md Amendment 3; Ajv can't compile inside n8n 2.x's hardened runner) |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` | Expressions read `$env.SALESFORCE_INSTANCE_URL`, `SLACK_APPROVAL_CHANNEL`, `SLACK_ALERT_CHANNEL` and `N8N_EDITOR_BASE_URL`. Code nodes never read `$env` |
| `WEBHOOK_URL=https://<tunnel>/` | The Slack Approve and Reject buttons are links to `WEBHOOK_URL/webhook-waiting/...`, so this must be publicly reachable |
| `N8N_EDITOR_BASE_URL` | Builds the execution link in alerts: `{N8N_EDITOR_BASE_URL}/workflow/{id}/executions/{executionId}` |

## 2. Credentials (create these first; the names must match exactly)

| Credential name | n8n type | Fields | Used by |
|---|---|---|---|
| `Hotline Postgres` | Postgres | host `postgres`, database/user/password = `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD`, port 5432, SSL disable | every Postgres node |
| `Hotline Ingress Token (header auth)` | Header Auth | Name `X-Hotline-Token`, Value = `INTERNAL_TOKEN` | Webhook |
| `Anthropic API (header x-api-key)` | Header Auth | Name `x-api-key`, Value = your Anthropic API key | Call Claude |
| `Salesforce OAuth2` | Salesforce OAuth2 API | Connected App (or External Client App) consumer key/secret; scopes `api refresh_token`; sign in as the integration user (PRD §6.6) | SF Upsert Lead, SF Create Task |
| `Slack Bot` | Slack API (Access Token) | Bot token `xoxb-...` with scope `chat:write`. Invite the bot to `#lead-approvals` and `#hotline-alerts` | Post Approval Header, Ask Approval, Reply Lead Link, Post Alert |

Each node stores its credential as `{id: <placeholder>, name: <name above>}`.
The placeholder IDs are `hotlinePostgres0`, `hotlineIngressTk`,
`hotlineAnthropic`, `hotlineSalesforc` and `hotlineSlackBot0`. You can link the
credentials in either of two ways:

- **UI (simplest).** Create the five credentials in the n8n UI, then import the
  workflows. Open each workflow. Any node with a credential warning needs you
  to pick the credential with the same name, then save.
- **CLI, no clicking.** Write a local, git-ignored `creds.json` that uses the
  placeholder IDs above: `[{"id":"hotlinePostgres0","name":"Hotline
  Postgres","type":"postgres","data":{...}}, ...]`. Import it with
  `n8n import:credentials --input=creds.json` before importing the workflows.
  Every node links automatically. The e2e test below was set up this way.
  Salesforce still needs one OAuth "Connect" click in the UI afterwards.

## 3. Import and publish

```bash
docker compose up -d --build
docker compose --profile setup run --rm n8n-import      # import:workflow --separate --input=/workflows
docker compose exec n8n n8n publish:workflow --id=HtlnClaudeExtrct
docker compose exec n8n n8n publish:workflow --id=HtlnErrorAlert00
docker compose exec n8n n8n publish:workflow --id=HtlnPostCall0000
docker compose restart n8n                              # publish via CLI takes effect on restart
```

In the UI, you can use Publish / Active on each of the three workflows instead.
All three must be published: the webhook only listens on a published workflow,
and n8n 2.x runs the published version of sub-workflows. If you re-import after
changing a JSON file, publish and restart again, or n8n keeps running the
previously published version.

**Error workflow.** `wf_post_call` → Settings → Error workflow is already set
to `subwf_error_alert` (ID `HtlnErrorAlert00`). If you import through the UI
(Import from File) instead of the CLI, n8n assigns new IDs, so re-select these
three things:
1. Settings → Error workflow → `subwf_error_alert`.
2. The sub-workflow in "Extract" → `subwf_claude_extract`.
3. The sub-workflow in "Alert Extract Failed", "Alert Approval Expired" and
   "Alert Salesforce Failed" → `subwf_error_alert`.

**Smoke test.** Without the token, the webhook refuses the request (PRD §8):

```bash
curl -i -X POST http://localhost:5678/webhook/hotline/post-call   # -> 403
```

## 4. Configuration (the "Config" node in `wf_post_call`)

| Key | Default | Notes |
|---|---|---|
| `INTENT_APPROVAL_THRESHOLD` | 7 | Score at or above this goes to Slack approval |
| `PROMPT_DIR` | `lead_qualification/v1` | Folder under `/prompts` |
| `MODEL` | `claude-sonnet-5` | Per-model request keys come from `/prompts/model_params.json` |
| `APPROVAL_TIMEOUT_H` | 24 | For the F3.3 two-minute test, set the value to the expression `{{ 2/60 }}` |
| `MIN_USER_TURNS` | 2 | Fewer user turns means `LOGGED_ONLY` with no Claude call |
| `SF_API_VERSION` | `v62.0` | Any version ≥ v46.0 works: from v46.0, an external-id upsert returns 200 or 201 with the record `id` on both create and update. Set it to the latest version your org lists (`GET /services/data/`) |

## 5. How it behaves

**Call status flow.** Every `call_events.status` change is a guarded
`UPDATE … WHERE conversation_id=$1 AND status=<expected>`. Zero rows means
another run already moved the call, and this run stops.

- **First transition (race handling).** The Webhook replies 202 before the
  ingress records `FORWARDED`. So the first move out of the forward state
  accepts `status IN ('RECEIVED','FORWARDED')`: this covers "Log Too Short",
  "Mark Extracted" and "Mark Failed (Extract)" (contracts.md Amendment 2). All
  later transitions expect exactly one state: `EXTRACTED`, then
  `PENDING_APPROVAL`, then `WRITTEN`, `REJECTED` or `EXPIRED`.

**Idempotency and replays.**
- `decisions` is always an upsert on `conversation_id`.
- The Salesforce Lead is written with `PATCH /services/data/<ver>/sobjects/Lead/Conversation_Id__c/<conversation_id>`, an upsert on an external ID. The HTTP Request node uses the `Salesforce OAuth2` credential; the Salesforce node's own upsert does not take the external ID in the URL.
- The Task is skipped when `decisions.sf_task_id` is already set.
- `extraction_runs` only appends.

**Failures.** Any path that ends in `FAILED` writes `last_error` as
`"<node>: <message>"`, then calls `subwf_error_alert`.

| Failure | Handling | `last_error` / result |
|---|---|---|
| Claude call fails | HTTP node retries twice, 5 s apart (30 s timeout), then continues | `Extract: Claude API error: …` |
| Output invalid twice | — | `Extract: Schema invalid after 2 attempt(s): …` |
| Salesforce 4xx | No retry | `SF Upsert Lead: 400 …` with the Salesforce error body |
| Salesforce 5xx or timeout | "SF Error" → "Retry SF?" → "Wait 5s" → the same node again, 2 retries at most | `FAILED` if the last retry fails |
| Approval timeout | Alert | `EXPIRED` |

Unhandled node errors reach `subwf_error_alert` through its Error Trigger.
There, the conversation ID is unknown, so the alert shows only the workflow,
node, error and link.

**Extraction request.** `subwf_claude_extract` builds the request exactly like
`eval/request.py`: same key order, same per-model profile, same retry message.
It validates exactly like `eval/validate.py`: same Ajv-style
`{path, message}` errors, sorted by path and then message.
- `@cfworker/json-schema` 4.1.1 has a quirk: with `shortCircuit=false`, it also
  reports declared properties that failed their own subschema as "additional".
  The normalizer keeps an `additionalProperties` error only when the property
  is not declared in the parent schema.

**Slack approval.**
1. "Post Approval Header" posts a short parent message.
2. "Ask Approval" (Send and Wait, Approve/Reject) posts the full details (name,
   company, score, evidence, budget, timeline, decision maker, summary) as a
   reply in that thread. The reply is also broadcast to the channel.
3. After approval, the Lead link `{SALESFORCE_INSTANCE_URL}/lightning/r/Lead/<id>/view` is posted in the same thread.

The approver's name is recorded only when Slack reports who clicked, which
happens in Send and Wait's "capture responder" mode. Otherwise `approver`
stays null.

## 6. What was verified

The following ran against a real stack: `hotline-n8n` built from
`n8n/Dockerfile`, Postgres 16 with `db/001_init.sql`, and a mock Claude and
Salesforce server. Only the Claude URL was rewritten, in a scratch copy.
- The workflows imported and published with `n8n import:workflow` and `publish:workflow`.
- The webhook returned 202 with the token, and 403 with no token or a bad one.
- Each scenario ended in the expected state:
  - Too-short call from `RECEIVED` → `LOGGED_ONLY`.
  - Score 5 → `WRITTEN`, with Lead and Task written.
  - Invalid-then-valid output → 2 `extraction_runs`, then `WRITTEN`. The retry
    body matched the contract byte for byte.
  - Invalid twice → `FAILED`.
  - Not a lead → `LOGGED_ONLY`.
  - Score 9 → `PENDING_APPROVAL`.
  - Claude 500 → 3 attempts, then `FAILED`.
  - Salesforce 400 → 1 attempt, then `FAILED` with the Salesforce body.
  - Salesforce 503, 503, 201 → `WRITTEN`.
  - Salesforce 503 three times → `FAILED`.
  - Replay of a `WRITTEN` call → the Lead was upserted again and the Task was skipped.
- The Validate node matched `eval/validate.py` on 12 invalid and edge-case
  instances.

Not exercised: the Slack side (real Slack and Salesforce OAuth). Approval,
reject and expiry routing were checked by reading the flow, not by a run.
