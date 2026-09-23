-- Hotline production monitoring queries (PRD §9.4, F4.4) and PII purge (§8).
-- Run a single query by copy/paste, or all of them:
--   docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < db/queries.sql
-- NOTE: running the whole file also runs the 30-day purge at the bottom.

-- ---------------------------------------------------------------------------
-- 1. Calls by status in the last 24 h ("today").
-- ---------------------------------------------------------------------------
SELECT status, count(*) AS calls
FROM hotline.call_events
WHERE received_at > now() - interval '24 hours'
GROUP BY status
ORDER BY 2 DESC;

-- ---------------------------------------------------------------------------
-- 2. Extraction retry rate and latency, last 7 days.
--    retry_rate = share of extraction_runs rows that are a retry (attempt > 1).
--    Latency is per Claude attempt (latency_ms recorded by the Validate node).
-- ---------------------------------------------------------------------------
SELECT count(*)                                                 AS runs,
       avg((attempt > 1)::int)                                  AS retry_rate,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM hotline.extraction_runs
WHERE created_at > now() - interval '7 days';

-- ---------------------------------------------------------------------------
-- 3. Same latency split by model and prompt version (compare v1/v2, Sonnet/Haiku).
-- ---------------------------------------------------------------------------
SELECT model, prompt_version,
       count(*)                                                 AS runs,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM hotline.extraction_runs
WHERE created_at > now() - interval '7 days'
GROUP BY model, prompt_version
ORDER BY model, prompt_version;

-- ---------------------------------------------------------------------------
-- 4. Schema-valid rate: first try vs after the one retry, per call, last 7 days.
--    Target (PRD §1): 100% after one retry.
-- ---------------------------------------------------------------------------
WITH per_call AS (
    SELECT conversation_id,
           bool_or(is_valid) FILTER (WHERE attempt = 1) AS valid_first_try,
           bool_or(is_valid)                            AS valid_after_retry
    FROM hotline.extraction_runs
    WHERE created_at > now() - interval '7 days'
    GROUP BY conversation_id
)
SELECT count(*)                                           AS calls_extracted,
       avg(coalesce(valid_first_try, false)::int)         AS valid_first_try_rate,
       avg(valid_after_retry::int)                        AS valid_after_retry_rate,
       count(*) FILTER (WHERE NOT valid_after_retry)      AS invalid_after_retry
FROM per_call;

-- ---------------------------------------------------------------------------
-- 5. Approval turnaround (Slack gate). Pending approvals have NULL outcome.
-- ---------------------------------------------------------------------------
SELECT approval_outcome,
       count(*)                                     AS decisions,
       avg(decided_at - created_at)                 AS avg_wait,
       percentile_cont(0.95) WITHIN GROUP (
           ORDER BY extract(epoch FROM decided_at - created_at)) AS p95_wait_s
FROM hotline.decisions
WHERE route = 'APPROVAL'
GROUP BY approval_outcome
ORDER BY approval_outcome NULLS LAST;

-- ---------------------------------------------------------------------------
-- 6. Hang-up to Salesforce write latency, auto path, last 7 days. Target p95 < 60 s.
--    decisions.decided_at is set when the Salesforce write succeeds.
-- ---------------------------------------------------------------------------
SELECT count(*) AS auto_writes,
       percentile_cont(0.5)  WITHIN GROUP (
           ORDER BY extract(epoch FROM d.decided_at - e.received_at)) AS p50_s,
       percentile_cont(0.95) WITHIN GROUP (
           ORDER BY extract(epoch FROM d.decided_at - e.received_at)) AS p95_s
FROM hotline.decisions d
JOIN hotline.call_events e USING (conversation_id)
WHERE d.route = 'AUTO_WRITE'
  AND d.decided_at > now() - interval '7 days';

-- ---------------------------------------------------------------------------
-- 7. Failures by node, last 7 days. last_error is "<node name>: <message>"
--    (ingress forward failures are "forward: <message>"), per contracts.md.
-- ---------------------------------------------------------------------------
SELECT status,
       split_part(coalesce(last_error, '(none)'), ':', 1) AS node,
       count(*)                                           AS failures,
       max(updated_at)                                    AS last_seen
FROM hotline.call_events
WHERE status IN ('FAILED', 'FORWARD_FAILED')
  AND updated_at > now() - interval '7 days'
GROUP BY status, node
ORDER BY failures DESC;

-- ---------------------------------------------------------------------------
-- 7b. Stuck calls (replay candidates, contracts.md Amendment 2): RECEIVED for
--     more than 5 min (ingress died before forwarding), FORWARDED/EXTRACTED for
--     more than 5 min (workflow died mid-run), PENDING_APPROVAL past the 24 h
--     approval timeout.
-- ---------------------------------------------------------------------------
SELECT conversation_id, status, received_at, updated_at,
       now() - updated_at AS stuck_for
FROM hotline.call_events
WHERE (status IN ('RECEIVED', 'FORWARDED', 'EXTRACTED')
       AND updated_at < now() - interval '5 minutes')
   OR (status = 'PENDING_APPROVAL'
       AND updated_at < now() - interval '25 hours')
ORDER BY updated_at;

-- ---------------------------------------------------------------------------
-- 8. Routing mix, last 7 days (how many calls go to each route).
-- ---------------------------------------------------------------------------
SELECT route, count(*) AS calls, avg(intent_score) AS avg_intent_score
FROM hotline.decisions
WHERE created_at > now() - interval '7 days'
GROUP BY route
ORDER BY 2 DESC;

-- ---------------------------------------------------------------------------
-- 9. Token usage and cost inputs per model, last 30 days, normalized per 100
--    extracted calls (PRD §9.2 "cost per 100 calls"). Fill the per-MTok list
--    prices in the prices CTE from Anthropic's current price list; while they
--    are NULL the est_cost columns are NULL.
-- ---------------------------------------------------------------------------
WITH prices (model, usd_per_mtok_in, usd_per_mtok_out) AS (
    VALUES ('claude-sonnet-5',           NULL::numeric, NULL::numeric),
           ('claude-haiku-4-5-20251001', NULL::numeric, NULL::numeric)
), usage AS (
    SELECT model,
           count(DISTINCT conversation_id)   AS calls,
           count(*)                          AS runs,
           sum(coalesce(input_tokens, 0))    AS input_tokens,
           sum(coalesce(output_tokens, 0))   AS output_tokens
    FROM hotline.extraction_runs
    WHERE created_at > now() - interval '30 days'
    GROUP BY model
)
SELECT u.model, u.calls, u.runs, u.input_tokens, u.output_tokens,
       round(u.input_tokens  * 100.0 / nullif(u.calls, 0)) AS input_tokens_per_100_calls,
       round(u.output_tokens * 100.0 / nullif(u.calls, 0)) AS output_tokens_per_100_calls,
       round((u.input_tokens  * p.usd_per_mtok_in
            + u.output_tokens * p.usd_per_mtok_out) / 1e6, 4)  AS est_cost_usd,
       round((u.input_tokens  * p.usd_per_mtok_in
            + u.output_tokens * p.usd_per_mtok_out) / 1e6
            * 100.0 / nullif(u.calls, 0), 4)                   AS est_cost_usd_per_100_calls
FROM usage u
LEFT JOIN prices p USING (model)
ORDER BY u.model;

-- ---------------------------------------------------------------------------
-- 10. PII retention purge (PRD §8): transcripts are kept 30 days.
--     Deletes children first (FK-safe): extraction_runs, decisions, then
--     call_events. Run daily (e.g. cron / n8n Schedule). Destructive.
-- ---------------------------------------------------------------------------
BEGIN;

DELETE FROM hotline.extraction_runs r
USING hotline.call_events e
WHERE r.conversation_id = e.conversation_id
  AND e.received_at < now() - interval '30 days';

DELETE FROM hotline.decisions d
USING hotline.call_events e
WHERE d.conversation_id = e.conversation_id
  AND e.received_at < now() - interval '30 days';

DELETE FROM hotline.call_events
WHERE received_at < now() - interval '30 days';

COMMIT;
