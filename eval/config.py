"""Eval constants: pricing, defaults, paths.

Pricing is Anthropic first-party list price in USD per million tokens
(input, output). Source: Anthropic model pricing table as cached in the
claude-api skill (cache date 2026-06-24), looked up 2026-09-23:
    Claude Sonnet 5   $2.00 / $10.00
    Claude Haiku 4.5  $1.00 / $5.00
Re-check https://www.anthropic.com/pricing before publishing numbers.
Prompt-caching and batch discounts are not modelled (the pipeline uses neither).
"""

from __future__ import annotations

import os
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
DATASET_PATH = EVAL_DIR / "dataset.jsonl"
RESULTS_DIR = EVAL_DIR / "results"

DEFAULT_MODEL = os.environ.get("EVAL_MODEL") or "claude-sonnet-5"
DEFAULT_PROMPT = "lead_qualification/v1"

# Must equal the n8n Config node (docs/contracts.md "Routing").
INTENT_APPROVAL_THRESHOLD = 7
MIN_USER_TURNS = 2

# $ per million tokens: (input, output)
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def price_for(model: str) -> tuple[float, float] | None:
    return PRICING_PER_MTOK.get(model)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    p = price_for(model)
    if p is None:
        return None
    return (input_tokens * p[0] + output_tokens * p[1]) / 1_000_000
