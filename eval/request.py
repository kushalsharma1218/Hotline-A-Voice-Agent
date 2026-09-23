"""Single shared builder for the Claude Messages request (docs/contracts.md,
including Amendment 1).

The n8n Code node in ``subwf_claude_extract`` mirrors this file. If you change
the shape here, change it there too; the eval only measures what production
runs if the two stay identical.

Request (attempt 1), keys in exactly this order::

    {
      "model": <model>,
      "max_tokens": 1024,
      "temperature": ...,   # only if the model's profile has it
      "thinking": ...,      # only if the model's profile has it
      "system": <system.md, verbatim>,
      "tools": [{"name": <meta.tool_name>,
                 "description": TOOL_DESCRIPTION,
                 "input_schema": <schema.json minus top-level "$schema" and "$id">}],
      "tool_choice": {"type": "tool", "name": <meta.tool_name>},
      "messages": [{"role": "user",
                    "content": "<transcript>\\n" + transcript_text + "\\n</transcript>"}]
    }

Retry (attempt 2) = the attempt-1 request plus two messages:

1. ``{"role": "assistant", "content": <attempt-1 response content array, unchanged>}``
2. ``{"role": "user", "content": [{"type": "tool_result", "tool_use_id": <id>,
   "is_error": true, "content": "Schema validation failed:\\n- <path>: <message>\\n- ..."}]}``

``<path>`` is the Ajv-style instancePath (JSON pointer, ``""`` for the root,
rendered as ``/``); see ``validate.py``.

Per-model profile (Amendment 1): ``prompts/model_params.json`` maps a model id
to the extra keys it needs. It is the single source of truth; n8n reads the
same file from ``/prompts/model_params.json``::

    claude-sonnet-5            {"thinking": {"type": "disabled"}}  (rejects temperature)
    claude-haiku-4-5[-2025..]  {"temperature": 0}
    any other model            no extra keys

Only ``temperature`` and ``thinking`` are applied, in that order.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_ROOT = REPO_ROOT / "prompts"

MODEL_PARAMS_PATH = PROMPTS_ROOT / "model_params.json"
MAX_TOKENS = 1024
PROFILE_KEYS = ("temperature", "thinking")  # merge order after max_tokens
TOOL_DESCRIPTION = "Record the qualified lead extracted from the call transcript."


@dataclass(frozen=True)
class Prompt:
    prompt_dir: str      # e.g. "lead_qualification/v1"
    system: str          # system.md contents, verbatim
    schema: dict         # schema.json as written (with $schema / $id)
    meta: dict           # {"version": ..., "tool_name": ...}

    @property
    def version(self) -> str:
        return self.meta["version"]

    @property
    def tool_name(self) -> str:
        return self.meta["tool_name"]


def load_model_params(path: Path | str = MODEL_PARAMS_PATH) -> dict[str, dict]:
    """The model -> extra-keys map from prompts/model_params.json."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


MODEL_PARAMS: dict[str, dict] = load_model_params()


def model_params(model: str, params: dict[str, dict] | None = None) -> dict:
    """Profile keys for ``model`` in merge order; {} for unknown models."""
    profile = (MODEL_PARAMS if params is None else params).get(model, {})
    return {k: copy.deepcopy(profile[k]) for k in PROFILE_KEYS if k in profile}


def load_prompt(prompt_dir: str, prompts_root: Path | str = PROMPTS_ROOT) -> Prompt:
    """Read system.md, schema.json and meta.json from ``prompts/<prompt_dir>``."""
    base = Path(prompts_root) / prompt_dir
    system = (base / "system.md").read_text(encoding="utf-8")
    schema = json.loads((base / "schema.json").read_text(encoding="utf-8"))
    meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
    return Prompt(prompt_dir=prompt_dir, system=system, schema=schema, meta=meta)


def tool_input_schema(schema: dict) -> dict:
    """The schema with the top-level ``$schema`` and ``$id`` keys removed."""
    return {k: v for k, v in schema.items() if k not in ("$schema", "$id")}


def wrap_transcript(transcript_text: str) -> str:
    return f"<transcript>\n{transcript_text}\n</transcript>"


def build_request(prompt: Prompt, transcript_text: str, model: str) -> dict:
    """Attempt-1 request body, exactly the contracts shape (with Amendment 1)."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        **model_params(model),
        "system": prompt.system,
        "tools": [
            {
                "name": prompt.tool_name,
                "description": TOOL_DESCRIPTION,
                "input_schema": tool_input_schema(prompt.schema),
            }
        ],
        "tool_choice": {"type": "tool", "name": prompt.tool_name},
        "messages": [{"role": "user", "content": wrap_transcript(transcript_text)}],
    }


def format_errors(errors: list[dict]) -> str:
    """``Schema validation failed:\\n- <path>: <message>`` one line per error."""
    lines = [f"- {e['path'] or '/'}: {e['message']}" for e in errors]
    return "Schema validation failed:\n" + "\n".join(lines)


def build_retry_request(
    first_request: dict,
    response_content: list[dict],
    tool_use_id: str,
    errors: list[dict],
) -> dict:
    """Attempt-2 request: attempt 1 + assistant turn + error tool_result."""
    retry = copy.deepcopy(first_request)
    retry["messages"].append(
        {"role": "assistant", "content": copy.deepcopy(response_content)}
    )
    retry["messages"].append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": format_errors(errors),
                }
            ],
        }
    )
    return retry
