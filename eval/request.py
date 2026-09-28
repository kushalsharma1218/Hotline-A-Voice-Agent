"""Single shared builder for the Gemini generateContent request (docs/contracts.md,
including Amendments 1 and 4).

The n8n Code node in ``subwf_llm_extract`` mirrors this file. If you change
the shape here, change it there too; the eval only measures what production
runs if the two stay identical.

The model is not in the body; it goes in the URL
(``POST /v1beta/models/<model>:generateContent``). Request (attempt 1), keys in
exactly this order::

    {
      "systemInstruction": {"parts": [{"text": <system.md, verbatim>}]},
      "contents": [{"role": "user", "parts": [{"text":
                    "<transcript>\\n" + transcript_text + "\\n</transcript>"}]}],
      "tools": [{"functionDeclarations": [{
                  "name": <meta.tool_name>,
                  "description": TOOL_DESCRIPTION,
                  "parameters": gemini_schema(<schema.json>)}]}],
      "toolConfig": {"functionCallingConfig": {"mode": "ANY",
                     "allowedFunctionNames": [<meta.tool_name>]}},
      "generationConfig": {"maxOutputTokens": 1024, ...model profile}
    }

``gemini_schema`` converts the JSON Schema to Gemini's OpenAPI-style subset
(upper-case types, ``["string", "null"]`` -> ``nullable``, unsupported keywords
such as ``additionalProperties`` dropped). The model's output is still
validated against the full JSON Schema, so dropped keywords are enforced there.

Retry (attempt 2) = the attempt-1 request plus two ``contents`` entries:

1. ``{"role": "model", "parts": <attempt-1 candidate parts, unchanged>}``
2. ``{"role": "user", "parts": [{"functionResponse": {"name": <tool_name>,
   ["id": <call id, if the call had one>,] "response": {"error":
   "Schema validation failed:\\n- <path>: <message>\\n- ..."}}}]}``

``<path>`` is the Ajv-style instancePath (JSON pointer, ``""`` for the root,
rendered as ``/``); see ``validate.py``.

Per-model profile (Amendment 1): ``prompts/model_params.json`` maps a model id
to extra ``generationConfig`` keys. It is the single source of truth; n8n reads
the same file from ``/prompts/model_params.json``::

    gemini-3.8-flash,
    gemini-2.5-flash[-lite]  {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}}
    any other model          no extra keys

Only ``temperature`` and ``thinkingConfig`` are applied, in that order.
Thinking is off because thinking tokens count against ``maxOutputTokens``.
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
PROFILE_KEYS = ("temperature", "thinkingConfig")  # merge order after maxOutputTokens
TOOL_DESCRIPTION = "Record the qualified lead extracted from the call transcript."

# Gemini Schema fields kept by gemini_schema(); everything else is dropped.
GEMINI_SCHEMA_KEYS = ("type", "nullable", "title", "description", "enum", "format",
                      "properties", "required", "items", "minItems", "maxItems",
                      "minLength", "maxLength", "minimum", "maximum")


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


def gemini_schema(schema: dict) -> dict:
    """JSON Schema -> Gemini function ``parameters`` schema (see module docstring)."""
    out: dict = {}
    for key in GEMINI_SCHEMA_KEYS:
        if key not in schema:
            continue
        val = schema[key]
        if key == "type":
            types = val if isinstance(val, list) else [val]
            non_null = [t for t in types if t != "null"]
            if len(non_null) != 1:
                raise ValueError(f"unsupported type for Gemini schema: {val!r}")
            out["type"] = non_null[0].upper()
            if "null" in types:
                out["nullable"] = True
        elif key == "properties":
            out[key] = {name: gemini_schema(sub) for name, sub in val.items()}
        elif key == "items":
            out[key] = gemini_schema(val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def wrap_transcript(transcript_text: str) -> str:
    return f"<transcript>\n{transcript_text}\n</transcript>"


def build_request(prompt: Prompt, transcript_text: str, model: str) -> dict:
    """Attempt-1 request body, exactly the contracts shape (Amendments 1 and 4)."""
    return {
        "systemInstruction": {"parts": [{"text": prompt.system}]},
        "contents": [{"role": "user", "parts": [{"text": wrap_transcript(transcript_text)}]}],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": prompt.tool_name,
                        "description": TOOL_DESCRIPTION,
                        "parameters": gemini_schema(prompt.schema),
                    }
                ]
            }
        ],
        "toolConfig": {
            "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [prompt.tool_name]}
        },
        "generationConfig": {"maxOutputTokens": MAX_TOKENS, **model_params(model)},
    }


def format_errors(errors: list[dict]) -> str:
    """``Schema validation failed:\\n- <path>: <message>`` one line per error."""
    lines = [f"- {e['path'] or '/'}: {e['message']}" for e in errors]
    return "Schema validation failed:\n" + "\n".join(lines)


def build_retry_request(
    first_request: dict,
    response_parts: list[dict],
    tool_name: str,
    errors: list[dict],
    call_id: str | None = None,
) -> dict:
    """Attempt-2 request: attempt 1 + model turn + error functionResponse."""
    retry = copy.deepcopy(first_request)
    retry["contents"].append({"role": "model", "parts": copy.deepcopy(response_parts)})
    function_response: dict = {"name": tool_name}
    if call_id:
        function_response["id"] = call_id
    function_response["response"] = {"error": format_errors(errors)}
    retry["contents"].append({"role": "user", "parts": [{"functionResponse": function_response}]})
    return retry


# ------------------------------------------------------------- response parsing

def parse_response(resp: dict, tool_name: str) -> dict:
    """Pull what the extractor needs out of a generateContent response.

    Returns ``parts`` (candidate 0's parts, for the retry), ``call`` (the first
    ``functionCall`` named ``tool_name`` or None), ``finish_reason`` (the
    candidate's finishReason, else promptFeedback.blockReason, else None) and
    token counts. Output tokens include thinking tokens, which are billed as
    output.
    """
    candidates = resp.get("candidates") or []
    cand = candidates[0] if candidates else {}
    parts = (cand.get("content") or {}).get("parts") or []
    call = next((p["functionCall"] for p in parts
                 if isinstance(p.get("functionCall"), dict)
                 and p["functionCall"].get("name") == tool_name), None)
    finish = cand.get("finishReason") or (resp.get("promptFeedback") or {}).get("blockReason")
    usage = resp.get("usageMetadata") or {}
    return {
        "parts": parts,
        "call": call,
        "finish_reason": finish,
        "input_tokens": usage.get("promptTokenCount"),
        "output_tokens": (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
        if usage else None,
    }


def incomplete_message(finish_reason: str | None) -> str:
    return f"no complete function call (finish_reason={finish_reason})"
