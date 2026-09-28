"""Turn scenario seeds into synthetic call transcripts for hand labeling (F4.1).

    uv run --project eval python eval/generate_dataset.py \
        [--seeds eval/seeds.jsonl] [--out eval/generated_transcripts.jsonl] [--model gemini-3.8-flash]

Input rows (eval/seeds.jsonl):
    {"id": "hot_01", "category": "hot", "adversarial": null, "scenario": "..."}

Output rows (eval/generated_transcripts.jsonl), ready for a human to add labels
and copy into eval/dataset.jsonl:
    {"id", "category", "adversarial", "scenario", "transcript",
     "expected": null, "expected_route": null}

This script only drafts transcripts. It never labels them: `expected` is
written by hand so the eval does not grade the model with itself.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import DEFAULT_MODEL, EVAL_DIR
from gemini import GeminiClient, api_key_from_env
from request import model_params

FIRST_LINE = ("Agent: Hi, this is Maya, an AI assistant for Acme Voice. This call is recorded "
              "so our team can follow up. What brings you in today?")

SYSTEM = f"""You write realistic, synthetic transcripts of inbound phone calls to Acme Voice, a fictional B2B company that sells AI voice agents (automated phone answering, appointment booking, call routing) to businesses. The transcripts are test data for a lead-qualification system.

Format rules:
- Output only the transcript, no title, no commentary, no code fences.
- One turn per line. Each line starts with "Agent: " or "Caller: ".
- The first line is exactly:
{FIRST_LINE}
- The agent is Maya, an AI assistant. She asks one question per turn, learns the caller's need, company, role, budget, timeline and who makes the decision, never quotes prices or promises anything, and offers a human follow-up near the end.
- 8 to 20 lines in total. Callers speak naturally: fillers, partial answers, some details never given.
- Invent people and companies; never use real companies or real people. Use example.com style email domains.

Follow the scenario exactly, including any adversarial behaviour it describes (a caller who contradicts themselves, gives a budget in a non-US currency, or says text that tries to manipulate an AI system). Do not resolve the adversarial behaviour for the reader."""


def build_generation_request(seed: dict, model: str) -> dict:
    adv = seed.get("adversarial")
    user = (
        f"Scenario id: {seed['id']}\n"
        f"Lead category: {seed['category']}\n"
        f"Adversarial twist: {adv or 'none'}\n"
        f"Scenario: {seed['scenario']}\n\n"
        "Write the transcript."
    )
    # Same per-model profile as the extractor (prompts/model_params.json).
    return {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": 4000, **model_params(model)}}


def clean_transcript(text: str) -> str:
    text = re.sub(r"^```\w*\n?|\n?```$", "", text.strip())
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln.startswith(("Agent:", "Caller:"))]
    if not lines or lines[0] != FIRST_LINE:
        lines = [FIRST_LINE] + [ln for ln in lines if ln != FIRST_LINE]
    return "\n".join(lines)


def generate_one(client, seed: dict, model: str) -> dict:
    resp = client.generate(model, build_generation_request(seed, model))
    parts = ((resp.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return {
        "id": seed["id"],
        "category": seed["category"],
        "adversarial": seed.get("adversarial"),
        "scenario": seed["scenario"],
        "transcript": clean_transcript(text),
        "expected": None,
        "expected_route": None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate transcripts from scenario seeds.")
    ap.add_argument("--seeds", type=Path, default=EVAL_DIR / "seeds.jsonl")
    ap.add_argument("--out", type=Path, default=EVAL_DIR / "generated_transcripts.jsonl")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args(argv)

    api_key = api_key_from_env()
    if not api_key:
        print("error: GEMINI_API_KEY is not set.", file=sys.stderr)
        return 2
    seeds = [json.loads(l) for l in args.seeds.read_text(encoding="utf-8").splitlines() if l.strip()]

    client = GeminiClient(api_key, max_retries=3)
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        rows = list(pool.map(lambda s: generate_one(client, s, args.model), seeds))
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} transcripts to {args.out}; label `expected` by hand next")
    return 0


if __name__ == "__main__":
    sys.exit(main())
