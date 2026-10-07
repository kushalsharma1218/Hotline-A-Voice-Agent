# Hotline

Hotline is a voice agent that handles inbound sales calls, qualifies prospects, and records the outcome in Salesforce.

A caller speaks with an ElevenLabs voice agent. After the call ends, the transcript is sent through a Python ingress service and processed by an n8n workflow. Claude extracts a structured lead from the conversation, and n8n decides what happens next:

- **Not a lead:** log the call.
- **Cold or warm lead:** create or update the lead in Salesforce automatically.
- **Hot lead:** send it to Slack for one-click approval before writing to Salesforce.

Every extraction and routing decision is recorded in Postgres. The same prompts, schemas, and request logic are used by the evaluation harness so changes can be measured consistently.

The core design principle is simple:

> **Claude extracts structured information. n8n owns decisions and side effects.**

## Contents

1. [Architecture](#architecture)
2. [Design decisions](#design-decisions)
3. [Guardrails](#guardrails)
4. [Evaluation](#evaluation)
5. [Running locally](#running-locally)
6. [Future additions](#future-additions)
7. [Repository layout](#repository-layout)

---

## Architecture

```mermaid
flowchart LR
    C[Caller] --> EA[ElevenLabs<br/>Voice Agent]
    EA -->|post-call webhook| IN[Ingress<br/>FastAPI]

    IN --> DB[(Postgres<br/>hotline schema)]
    IN -->|internal token| WF[n8n<br/>wf_post_call]

    WF --> EX[n8n<br/>subwf_claude_extract]
    EX --> CL[Claude API]

    WF --> SF[Salesforce<br/>Lead + Task]
    WF --> SA[Slack<br/>#lead-approvals]
    WF --> ER[n8n<br/>subwf_error_alert]

    ER --> SL[Slack<br/>#hotline-alerts]

    WF --> DB

    EV[Eval harness<br/>Python] --> CL
    PR[prompts/ + schema] --> EX
    PR --> EV
```

### Call flow

1. **The caller reaches the ElevenLabs voice agent.**

   The agent qualifies the caller through a natural voice conversation.

2. **ElevenLabs sends the completed call to the ingress.**

   After the call ends, ElevenLabs sends a signed `post_call_transcription` webhook to the FastAPI ingress.

3. **The ingress verifies and stores the event.**

   The ingress validates:

   - HMAC signature
   - webhook timestamp
   - configured agent ID

   The raw event is stored in `hotline.call_events`.

   `conversation_id` is the primary key, providing idempotency for repeated webhook deliveries.

   Once the event is persisted, the ingress immediately returns `200`.

4. **The transcript is forwarded to n8n.**

   The ingress flattens the transcript and forwards it to `wf_post_call` using an internal authentication token.

   The forwarding layer has retry handling, and calls can also be replayed through the admin endpoint.

5. **Claude extracts the lead.**

   `wf_post_call` calls the reusable `subwf_claude_extract` workflow.

   Claude receives the conversation and can call a single tool:

   ```text
   record_lead
   ```

   The tool's `input_schema` is the lead schema stored in the repository.

   The result is validated before the workflow continues. If validation fails, Claude receives the validation errors and gets one additional attempt.

   Each extraction attempt is recorded in `extraction_runs`.

6. **n8n determines the route.**

   Routing is deterministic:

   ```text
   Not a lead       → LOGGED_ONLY
   Score 1–6        → AUTO_WRITE
   Score 7 or more  → APPROVAL
   ```

7. **Salesforce or Slack handles the result.**

   Auto-write leads are upserted into Salesforce using `Conversation_Id__c` as the external ID.

   A call-summary Task is created alongside the Lead.

   Hot leads are sent to Slack for approval. Salesforce is updated only after approval.

8. **The workflow records the result.**

   Workflow state changes, extraction attempts, routing decisions, and failures are stored in Postgres.

   Operational failures are sent to `#hotline-alerts` with a link to the relevant n8n execution.

Detailed architecture diagrams, state transitions, component responsibilities, and interfaces are documented in:

```text
docs/architecture.md
docs/contracts.md
```

---

## Design decisions

| Decision | Implementation | Why |
|---|---|---|
| Webhook verification | Python ingress | Raw-body HMAC verification is easier to test and maintain in Python. |
| Webhook acknowledgement | Persist → return `200` → process asynchronously | ElevenLabs should not wait for downstream processing. |
| Idempotency | `conversation_id` primary key + Salesforce external ID | Retries and replays remain safe. |
| Structured extraction | Forced tool call + JSON Schema validation | Keeps model output constrained and validates the result before routing. |
| Extraction model | Claude | Prompt versions, model parameters, schemas, and evaluation stay under version control. |
| High-value leads | Slack approval for score `7+` | Adds an explicit human decision before high-value CRM writes. |
| CRM writes | n8n | The model never performs external side effects. |
| Prompt management | Versioned files in `prompts/` | Prompts can be reviewed, diffed, and evaluated independently. |
| Secrets | `.env` + n8n credential store | Credentials remain outside workflow definitions and source control. |

### Model-specific request parameters

Claude models can require different request parameters.

The request configuration is therefore stored centrally in:

```text
prompts/model_params.json
```

Both n8n and the evaluation harness use the same model configuration.

For example, the current profiles configure:

- `claude-sonnet-5` with thinking disabled for the forced tool-call workflow.
- `claude-haiku-4-5` with `temperature: 0`.

A response is treated as invalid when:

- the response reaches `max_tokens`;
- a `tool_use` block is missing; or
- the returned tool input fails schema validation.

The extraction workflow performs one schema retry.

### Replay and idempotency

The ingress supports replaying calls that require another processing attempt.

The replay operation first performs a guarded state transition back to `RECEIVED`.

If another replay has already claimed the call, the guarded update affects zero rows and the API returns `409`.

The workflow remains idempotent during replay:

- `decisions` are upserted;
- Salesforce Leads are upserted using `Conversation_Id__c`;
- existing Salesforce Tasks are detected through `sf_task_id`.

This allows the same call to move through the workflow again without creating duplicate CRM records.

### JSON Schema validation

The n8n Code node uses `@cfworker/json-schema` for validation.

This keeps validation compatible with n8n's sandboxed Code node environment without enabling insecure code-generation behaviour.

Validation errors are normalized into:

```json
{
  "path": "...",
  "message": "..."
}
```

The evaluation validator uses the same error format, so production extraction and evaluation exercise the same retry behaviour.

---

## Guardrails

The system keeps model output separate from system actions.

Claude produces structured lead information. Deterministic workflow logic decides whether anything is written.

| Risk | Protection | Implementation |
|---|---|---|
| Forged webhook | HMAC-SHA256 + constant-time comparison + timestamp validation + agent allowlist | `ingress/app/security.py` |
| Duplicate webhook | `conversation_id` primary key | `hotline.call_events` |
| Direct n8n access | `X-Hotline-Token` authentication | n8n webhook |
| Duplicate Salesforce Lead | External ID + upsert | `Conversation_Id__c` |
| Duplicate Salesforce Task | Existing Task check | `sf_task_id` |
| Unauthorized replay | Bearer `ADMIN_TOKEN` + guarded state transition | ingress |
| Prompt injection | Transcript treated as data + single forced tool | `prompts/lead_qualification/*` |
| Incorrect hot-lead write | Slack approval for score `7+` | `wf_post_call` |
| Invalid model output | JSON Schema validation | `subwf_claude_extract` |
| Excessive Salesforce access | Dedicated integration user | Salesforce |
| Secret exposure | `.env` + n8n credential store | configuration |
| Transcript retention | 30-day purge | `db/queries.sql` |
| Caller consent | AI disclosure + recording disclosure | `agent/agent_prompt.md` |
| Excessive API cost | Minimum caller-turn check + token limits + retries | n8n + eval |

### Webhook security

The ElevenLabs signature is calculated over:

```text
"{timestamp}.{raw_request_body}"
```

The ingress:

1. validates the timestamp;
2. verifies the HMAC signature;
3. checks the configured agent ID;
4. persists the event;
5. returns `200`.

The timestamp window is 30 minutes.

The security tests cover valid, forged, and stale signatures.

### n8n access

The n8n webhook requires:

```text
X-Hotline-Token: <INTERNAL_TOKEN>
```

For example:

```bash
curl -i -X POST \
  http://localhost:5678/webhook/hotline/post-call
```

Requests without the internal token are rejected.

### Prompt injection

The transcript is explicitly treated as untrusted conversation data.

The prompt wraps the conversation in:

```xml
<transcript>
...
</transcript>
```

The model is instructed not to treat transcript content as system instructions.

The extraction model also has access to only one tool:

```text
record_lead
```

The evaluation dataset contains a dedicated prompt-injection case:

```text
cold_02
```

The expected result is based on the actual conversation rather than instructions contained inside the transcript.

### Salesforce permissions

Hotline uses a dedicated Salesforce integration user.

The integration account is limited to the objects required by the workflow:

- Lead
- Task

This keeps the CRM integration separate from normal administrative accounts.

### PII retention

Raw transcripts and extraction audit data are retained for 30 days.

The purge query in:

```text
db/queries.sql
```

removes:

- `call_events`
- `extraction_runs`
- `decisions`

inside a single transaction.

The purge can be scheduled through cron or an n8n Schedule trigger.

Salesforce retains the Lead and call summary according to the CRM's configured retention policy.

### Cost controls

The workflow avoids Claude calls for conversations with fewer than two caller turns.

The extraction path also uses:

- `max_tokens = 1024`
- limited HTTP retries
- one schema retry

Token usage is recorded in `extraction_runs` and can be used to calculate cost per 100 calls.

---

## Evaluation

The evaluation harness uses the same:

- prompt files;
- schemas;
- model parameters;
- request builder;
- validation logic;
- retry behaviour

as the production extraction workflow.

This makes prompt and model changes measurable before they are used in the workflow.

### Dataset

The evaluation dataset contains 20 synthetic, hand-labelled conversations.

| Type | Count |
|---|---:|
| Not a lead | 4 |
| Cold | 6 |
| Warm | 5 |
| Hot | 5 |
| **Total** | **20** |

Expected routing:

| Route | Count |
|---|---:|
| `LOGGED_ONLY` | 4 |
| `AUTO_WRITE` | 11 |
| `APPROVAL` | 5 |

The dataset includes three focused cases:

- `cold_02` — prompt injection
- `warm_03` — contradictory caller statements
- `hot_04` — budget provided in INR

The conversations are generated from:

```text
eval/seeds.jsonl
```

and labelled in:

```text
eval/dataset.jsonl
```

### Metrics

| Metric | Definition | Target |
|---|---|---:|
| Routing accuracy | Predicted route equals expected route | ≥ 90% |
| Score accuracy | Absolute score difference ≤ 1 | ≥ 85% |
| Enum fields | Exact match | ≥ 90% |
| Boolean fields | Exact match | ≥ 90% |
| Contact fields | Case-insensitive, trimmed match | ≥ 90% |
| Budget | Within 10% of expected or both null | ≥ 85% |
| Schema validity | Valid on first attempt / after retry | Report both |
| Injection resistance | Adversarial case follows expected label | Pass |
| Latency | p50 and p95 | Report |
| Cost | Input + output tokens | Per 100 calls |

Free-text fields such as:

```text
call_summary
need_summary
intent_evidence
```

are reviewed separately rather than automatically scored.

Five generated summaries are manually spot-checked as part of the evaluation process.

### Running the evaluation

From the repository root:

```bash
export ANTHROPIC_API_KEY=sk-ant-...

uv run --project eval python eval/run_eval.py \
  --prompt lead_qualification/v1 \
  --model claude-sonnet-5

uv run --project eval python eval/run_eval.py \
  --prompt lead_qualification/v2 \
  --model claude-sonnet-5

uv run --project eval python eval/run_eval.py \
  --prompt lead_qualification/v2 \
  --model claude-haiku-4-5-20251001

uv run --project eval python eval/report.py
```

The evaluation key is read from the environment.

Each run produces:

```text
eval/results/<prompt>__<model>.json
```

Optional arguments:

```bash
--concurrency N
--limit N
```

The default concurrency is `4`.

### Prompt versions

v2 focuses on several areas where lead qualification benefits from more explicit rules:

- currency normalization;
- handling caller corrections;
- prompt-injection resistance;
- scoring guidance;
- examples around the 6/7 routing boundary.

The measured results from each prompt/model combination are generated by `eval/report.py`.

### Evaluation results

| Prompt | Model | Routing | Score ±1 | Fields avg | Valid first try | p95 latency | Cost / 100 calls |
|---|---|---:|---:|---:|---:|---:|---:|
| v1 | claude-sonnet-5 | — | — | — | — | — | — |
| v2 | claude-sonnet-5 | — | — | — | — | — | — |
| v2 | claude-haiku-4-5 | — | — | — | — | — | — |

---

## Running locally

### Prerequisites

You need:

- Docker Desktop with Compose v2
- Python
- `uv`
- Anthropic API access
- ElevenLabs
- Salesforce Developer Edition
- Slack workspace
- two public HTTPS endpoints for local development

The local stack uses:

```text
Ingress → port 8000
n8n     → port 5678
```

Cloudflare Quick Tunnels can be used to expose both services.

### 1. Start the tunnels

```bash
cloudflared tunnel --url http://localhost:8000
```

and:

```bash
cloudflared tunnel --url http://localhost:5678
```

Save the HTTPS URLs for the next step.

Quick tunnel URLs change when the tunnel is restarted, so update `.env` when necessary.

### 2. Generate local secrets

From the repository root:

```bash
python scripts/gen_secrets.py
```

On Windows:

```powershell
py scripts/gen_secrets.py
```

The script creates `.env` from `.env.example` and generates:

```text
N8N_ENCRYPTION_KEY
INTERNAL_TOKEN
ADMIN_TOKEN
```

Each generated value is a random 64-character hexadecimal string.

Existing real values are preserved when the script is run again.

To generate a value manually:

```bash
openssl rand -hex 32
```

### 3. Configure `.env`

Configure the local environment with:

```text
WEBHOOK_URL=https://<n8n-tunnel>/
N8N_EDITOR_BASE_URL=https://<n8n-tunnel>

ELEVENLABS_AGENT_ID=...
ELEVENLABS_WEBHOOK_SECRET=...

SALESFORCE_INSTANCE_URL=https://<domain>.my.salesforce.com

ANTHROPIC_API_KEY=...
```

If port `5678` is already in use:

```text
N8N_HOST_PORT=5679
```

Then expose that port through the n8n tunnel.

The Docker network continues to use port `5678` internally.

### 4. Start the stack

```bash
docker compose up -d --build
```

Check the services:

```bash
docker compose ps
```

Check the ingress:

```bash
curl -s http://localhost:8000/healthz
```

Expected response:

```json
{
  "status": "ok",
  "db": "ok"
}
```

Import the workflows:

```bash
docker compose --profile setup run --rm n8n-import
```

The Postgres initialization script runs when the database volume is first created.

For a completely fresh local environment:

```bash
docker compose down -v
```

### 5. Configure n8n credentials

Open:

```text
http://localhost:5678
```

Create the n8n owner account and configure:

| Credential | Type | Purpose |
|---|---|---|
| `Hotline Postgres` | Postgres | Database access |
| `Hotline Ingress Token (header auth)` | Header Auth | Ingress authentication |
| `Anthropic API (header x-api-key)` | Header Auth | Claude API |
| `Salesforce OAuth2` | Salesforce OAuth2 API | Salesforce access |
| `Slack Bot` | Slack API | Slack notifications and approvals |

The credential names match the references in the exported workflows.

### 6. Publish the workflows

```bash
docker compose exec n8n n8n publish:workflow --id=HtlnClaudeExtrct

docker compose exec n8n n8n publish:workflow --id=HtlnErrorAlert00

docker compose exec n8n n8n publish:workflow --id=HtlnPostCall0000
```

Restart n8n:

```bash
docker compose restart n8n
```

The main workflow configuration is available in its `Config` node:

- approval threshold;
- prompt version;
- model;
- approval timeout.

### 7. Configure ElevenLabs

Agent configuration is documented in:

```text
agent/agent_prompt.md
```

Configure:

1. Agent first message.
2. System prompt.
3. Post-call webhook:
   ```text
   https://<ingress-tunnel>/webhooks/elevenlabs
   ```
4. Event:
   ```text
   post_call_transcription
   ```

Add the agent ID and webhook secret to `.env`:

```text
ELEVENLABS_AGENT_ID=...
ELEVENLABS_WEBHOOK_SECRET=...
```

Restart the ingress:

```bash
docker compose up -d ingress
```

### 8. Configure Salesforce

Create the following Lead fields.

#### Conversation ID

```text
Conversation_Id__c
```

Configuration:

- Text
- Length: 64
- External ID: enabled
- Unique: enabled

#### Intent score

```text
Intent_Score__c
```

Configuration:

- Number
- Precision/scale: `2,0`

Create an OAuth application with:

```text
api
refresh_token
```

Callback:

```text
https://<n8n-tunnel>/rest/oauth2-credential/callback
```

Use the exact callback displayed by the n8n Salesforce credential.

Create a dedicated Salesforce integration user with access to:

- Lead
- Task

Connect the account through the `Salesforce OAuth2` n8n credential.

### 9. Configure Slack

Create a Slack app with:

```text
chat:write
```

Install it into the workspace and configure the bot token in:

```text
Slack Bot
```

Create:

```text
#lead-approvals
#hotline-alerts
```

Invite the bot to both channels.

The channel names can be changed through:

```text
SLACK_APPROVAL_CHANNEL
SLACK_ALERT_CHANNEL
```

The approval workflow uses n8n's Send-and-Wait functionality.

### 10. Run a test call

A complete test can be started from the ElevenLabs agent interface.

For deterministic testing, use:

```bash
python scripts/send_test_webhook.py --help
```

The script builds a correctly signed ElevenLabs-style webhook from the evaluation dataset.

Monitor the system with:

```bash
docker compose logs -f ingress
```

and the n8n Executions view.

---

## Configuration reference

| Value | Source | Used by |
|---|---|---|
| Anthropic API key | Anthropic console | n8n + evaluation |
| ElevenLabs agent ID | ElevenLabs | ingress |
| ElevenLabs webhook secret | ElevenLabs | ingress |
| Salesforce instance URL | Salesforce My Domain | configuration |
| Salesforce consumer key | Salesforce app | n8n |
| Salesforce consumer secret | Salesforce app | n8n |
| Slack bot token | Slack app | n8n |
| `INTERNAL_TOKEN` | `gen_secrets.py` | ingress + n8n |
| `ADMIN_TOKEN` | `gen_secrets.py` | ingress |
| `N8N_ENCRYPTION_KEY` | `gen_secrets.py` | n8n |
| Postgres credentials | `.env.example` | Postgres + n8n |

Salesforce and Slack credentials are stored in n8n.

The `.env` file contains runtime configuration, ingress secrets, Postgres configuration, and the evaluation API key.

---

## Replay

Failed or interrupted processing can be restarted through the replay endpoint.

Set:

```bash
export ADMIN_TOKEN=...
```

Then:

```bash
curl -X POST \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8000/admin/replay/<conversation_id>
```

Replay supports calls in:

```text
FORWARD_FAILED
FAILED
RECEIVED
```

A `RECEIVED` call must be older than five minutes.

The state transition is guarded so concurrent replay requests cannot claim the same call.

---

## Tests

Run ingress tests:

```bash
cd ingress
uv run pytest
```

Run evaluation tests:

```bash
cd eval
uv run pytest
```

Integration tests can be run with Postgres:

```bash
cd ingress

DATABASE_URL=postgresql://hotline:hotline@localhost:5432/hotline \
uv run pytest -m integration
```

For the complete local stack:

```bash
bash scripts/smoke_test.sh
```

The smoke test covers webhook authentication, duplicate delivery, n8n authentication, short conversations, and replay authentication.

---

## Monitoring

`db/queries.sql` contains the operational queries for Hotline.

The queries cover:

- calls by status over the last 24 hours;
- extraction retry rate;
- p50/p95 extraction latency;
- latency by model and prompt;
- schema validity;
- approval turnaround;
- hang-up → Salesforce latency;
- workflow failures;
- stuck calls;
- routing distribution;
- token usage;
- cost per 100 calls;
- 30-day data cleanup.

Open Postgres with:

```bash
docker compose exec postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Run the required queries from `db/queries.sql`.

The final query performs the 30-day data purge.

---

## Repository layout

```text
.
├── README.md
├── docker-compose.yml
├── .env.example
│
├── agent/
│   └── agent_prompt.md
│
├── db/
│   ├── 001_init.sql
│   └── queries.sql
│
├── docs/
│   ├── architecture.md
│   └── contracts.md
│
├── eval/
│   ├── dataset.jsonl
│   ├── seeds.jsonl
│   ├── config.py
│   ├── generate_dataset.py
│   ├── request.py
│   ├── validate.py
│   ├── scoring.py
│   ├── run_eval.py
│   ├── report.py
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── results/
│   └── tests/
│
├── ingress/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── security.py
│   │   ├── models.py
│   │   ├── transcript.py
│   │   ├── store.py
│   │   └── forwarder.py
│   └── tests/
│
├── n8n/
│   ├── Dockerfile
│   └── workflows/
│       ├── README.md
│       ├── wf_post_call.json
│       ├── subwf_claude_extract.json
│       └── subwf_error_alert.json
│
├── prompts/
│   ├── model_params.json
│   └── lead_qualification/
│       ├── CHANGELOG.md
│       ├── v1/
│       │   ├── system.md
│       │   ├── schema.json
│       │   └── meta.json
│       └── v2/
│           ├── system.md
│           ├── schema.json
│           └── meta.json
│
└── scripts/
    ├── gen_secrets.py
    ├── smoke_test.sh
    └── send_test_webhook.py
```
