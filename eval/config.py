"""Eval constants: pricing, defaults, paths.

Pricing is the Gemini API paid-tier list price in USD per million tokens
(input, output), so the report shows what the pipeline would cost after
upgrading. On the free tier every call costs $0 (within the rate limits).
Paid-tier prices as last known, 2026-09-23:
    Gemini 2.5 Flash       $0.30 / $2.50
    Gemini 2.5 Flash-Lite  $0.10 / $0.40
Gemini 3.8 Flash (the default) has no price here yet, so its report shows cost as unknown.
Re-check https://ai.google.dev/gemini-api/docs/pricing before publishing numbers.
Context-caching and batch discounts are not modelled (the pipeline uses neither).
"""

from __future__ import annotations

import os
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
DATASET_PATH = EVAL_DIR / "dataset.jsonl"
RESULTS_DIR = EVAL_DIR / "results"

DEFAULT_MODEL = os.environ.get("EVAL_MODEL") or "gemini-3.8-flash"
DEFAULT_PROMPT = "lead_qualification/v1"

# Must equal the n8n Config node (docs/contracts.md "Routing").
INTENT_APPROVAL_THRESHOLD = 7
MIN_USER_TURNS = 2

# $ per million tokens: (input, output)
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}


def price_for(model: str) -> tuple[float, float] | None:
    return PRICING_PER_MTOK.get(model)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    p = price_for(model)
    if p is None:
        return None
    return (input_tokens * p[0] + output_tokens * p[1]) / 1_000_000
