"""Run a prompt version + model against the labeled dataset.

    uv run --project eval python eval/run_eval.py \
        --prompt lead_qualification/v1 --model gemini-3.8-flash --concurrency 2

Each case follows the production extractor (subwf_llm_extract): build the
request with ``request.build_request``, call Gemini, validate the function-call
arguments, and on failure retry once with the validation errors as a
``functionResponse``. Free-tier rate limits (429) are waited out by the client.
Writes ``eval/results/<prompt with / as _>__<model>.json`` (``__limit<N>`` is
appended for partial runs) and prints the PRD 9.3 table plus failures.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from config import DATASET_PATH, DEFAULT_MODEL, DEFAULT_PROMPT, RESULTS_DIR, price_for
from gemini import GeminiClient, api_key_from_env
from request import (Prompt, build_request, build_retry_request, incomplete_message,
                     load_prompt, parse_response)
from scoring import aggregate, score_case
from validate import validate


def load_dataset(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for k in ("id", "transcript", "expected", "expected_route"):
                if k not in row:
                    raise ValueError(f"{path}:{i}: row missing '{k}'")
            rows.append(row)
    return rows


def results_path(prompt_dir: str, model: str, limit: int | None) -> Path:
    name = f"{prompt_dir.replace('/', '_')}__{model}"
    if limit:
        name += f"__limit{limit}"
    return RESULTS_DIR / f"{name}.json"


def _call(client, model: str, req: dict, tool_name: str) -> tuple[dict, dict]:
    t0 = time.perf_counter()
    resp = client.generate(model, req)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    parsed = parse_response(resp, tool_name)
    info = {
        "latency_ms": latency_ms,
        "input_tokens": parsed["input_tokens"],
        "output_tokens": parsed["output_tokens"],
        "finish_reason": parsed["finish_reason"],
    }
    return parsed, info


def extract(client, prompt: Prompt, transcript: str, model: str) -> dict:
    """Run attempt 1 and, if invalid, attempt 2. Mirrors subwf_llm_extract.

    Invalid = schema errors, ``finish_reason == "MAX_TOKENS"``, or no
    functionCall. The retry answers the call with an error functionResponse;
    with no call to answer, it re-sends attempt 1 unchanged.
    """
    attempts: list[dict] = []
    req = build_request(prompt, transcript, model)
    output = None
    for attempt in (1, 2):
        try:
            parsed, info = _call(client, model, req, prompt.tool_name)
        except Exception as exc:  # API error after client retries: the call FAILS
            attempts.append({"attempt": attempt, "valid": False, "api_error": f"{type(exc).__name__}: {exc}"})
            break
        call = parsed["call"]
        if call is None or info["finish_reason"] == "MAX_TOKENS":
            # Amendment 1: truncated or missing function call = invalid output.
            errors = [{"path": "/", "message": incomplete_message(info["finish_reason"])}]
            candidate = call.get("args") if call is not None else None
        else:
            candidate = call.get("args", {})
            errors = validate(candidate, prompt.schema)
        attempts.append({"attempt": attempt, "valid": not errors, "errors": errors,
                         "output": candidate, **info})
        if not errors:
            output = candidate
            break
        if attempt == 1:
            if call is not None:
                req = build_retry_request(req, parsed["parts"], prompt.tool_name, errors, call.get("id"))
            # No function call to answer: re-send attempt 1 unchanged.
    return {
        "output": output,
        "attempts": attempts,
        "valid_first_try": bool(attempts and attempts[0]["valid"]),
        "valid_final": output is not None,
        "latency_ms": sum(a.get("latency_ms", 0) for a in attempts) or None,
        "input_tokens": sum(a.get("input_tokens") or 0 for a in attempts),
        "output_tokens": sum(a.get("output_tokens") or 0 for a in attempts),
    }


def run_case(client, prompt: Prompt, row: dict, model: str) -> dict:
    ex = extract(client, prompt, row["transcript"], model)
    return {
        "id": row["id"],
        "category": row.get("category"),
        "adversarial": row.get("adversarial"),
        "expected_route": row["expected_route"],
        "prediction": ex["output"],
        "scores": score_case(ex["output"], row["expected"], row["expected_route"]),
        **{k: ex[k] for k in ("valid_first_try", "valid_final", "latency_ms",
                              "input_tokens", "output_tokens", "attempts")},
    }


# ------------------------------------------------------------------- printing

def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def secs(ms: float | None) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.1f} s"


def money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.2f}"


TABLE_HEADER = ["Prompt", "Model", "Routing", "Score ±1", "Fields avg",
                "Valid first try", "p95 latency", "Cost / 100 calls"]


def table_row(prompt_version: str, model: str, m: dict) -> list[str]:
    short = prompt_version.split("@")[-1] if "@" in prompt_version else prompt_version
    return [short, model, pct(m["routing_accuracy"]), pct(m["score_within_1_accuracy"]),
            pct(m["fields_avg"]), pct(m["valid_first_try"]), secs(m["latency_ms"]["p95"]),
            money(m["cost_usd_per_100_calls"])]


def markdown_table(rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(TABLE_HEADER) + " |",
             "|" + "|".join("---" for _ in TABLE_HEADER) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def print_report(prompt: Prompt, model: str, cases: list[dict], m: dict) -> None:
    print()
    print(markdown_table([table_row(prompt.version, model, m)]))
    print()
    print(f"n={m['n']}  valid after retry {pct(m['valid_after_retry'])}  "
          f"p50 latency {secs(m['latency_ms']['p50'])}  "
          f"contact {pct(m['contact_accuracy'])}  budget amount {pct(m['budget_amount_accuracy'])}")
    inj = m["injection"]
    if inj["n"]:
        print(f"injection resistance: {inj['passed']}/{inj['n']} {'PASS' if inj['pass'] else 'FAIL'}")
    print("per-field accuracy: " + ", ".join(f"{f} {pct(v)}" for f, v in m["field_accuracy"].items()))

    failures = [c for c in cases
                if not c["scores"]["route"]["correct"]
                or c["scores"]["fields_correct"] < c["scores"]["fields_total"]]
    print(f"\nFailures ({len(failures)} of {len(cases)} cases):")
    for c in failures:
        r = c["scores"]["route"]
        route = "route ok" if r["correct"] else f"ROUTE {r['expected']} -> {r['predicted']}"
        tag = f" [{c['adversarial']}]" if c.get("adversarial") else ""
        print(f"- {c['id']}{tag}: {route}")
        if c["prediction"] is None:
            last = c["attempts"][-1] if c["attempts"] else {}
            print(f"    invalid output: {last.get('api_error') or last.get('errors')}")
            continue
        for f, v in c["scores"]["fields"].items():
            if not v["correct"]:
                print(f"    {f}: expected {v['expected']!r}, got {v['predicted']!r}")


# ------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="folder under prompts/, e.g. lead_qualification/v1")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=2,
                    help="parallel calls; keep low on the free tier (about 10 requests/minute)")
    ap.add_argument("--dataset", type=Path, default=DATASET_PATH)
    ap.add_argument("--limit", type=int, default=None, help="only the first N rows")
    args = ap.parse_args(argv)

    api_key = api_key_from_env()
    if not api_key:
        print("error: GEMINI_API_KEY is not set. Export it (see .env.example) and re-run.",
              file=sys.stderr)
        return 2
    if not args.dataset.exists():
        print(f"error: dataset not found: {args.dataset}", file=sys.stderr)
        return 2
    if price_for(args.model) is None:
        print(f"warning: no pricing for {args.model} in config.py; cost will be n/a", file=sys.stderr)

    prompt = load_prompt(args.prompt)
    rows = load_dataset(args.dataset)
    if args.limit:
        rows = rows[: args.limit]

    # max_retries=2 mirrors the n8n HTTP node (5xx retried twice).
    client = GeminiClient(api_key, max_retries=2, timeout=60.0)
    print(f"Running {len(rows)} cases: {prompt.version} on {args.model} (concurrency {args.concurrency})")
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        cases = list(pool.map(lambda r: run_case(client, prompt, r, args.model), rows))
    wall_s = time.perf_counter() - started

    metrics = aggregate(cases, model=args.model)
    out = {
        "prompt_dir": args.prompt,
        "prompt_version": prompt.version,
        "model": args.model,
        "dataset": str(args.dataset),
        "limit": args.limit,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wall_time_s": round(wall_s, 1),
        "metrics": metrics,
        "cases": cases,
    }
    path = results_path(args.prompt, args.model, args.limit)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print_report(prompt, args.model, cases, metrics)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
