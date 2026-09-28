import json

import pytest

import run_eval
from gemini import GeminiAPIError, endpoint
from request import (
    MAX_TOKENS,
    MODEL_PARAMS,
    MODEL_PARAMS_PATH,
    TOOL_DESCRIPTION,
    build_request,
    build_retry_request,
    format_errors,
    gemini_schema,
    load_prompt,
    model_params,
    parse_response,
)
from generate_dataset import build_generation_request
from run_eval import extract, load_dataset, results_path

VERSIONS = ["lead_qualification/v1", "lead_qualification/v2"]
TRANSCRIPT = "Agent: Hi, this is Maya.\nCaller: We need a receptionist bot."
MODEL = "gemini-2.5-flash"


@pytest.mark.parametrize("version", VERSIONS)
def test_load_prompt(version):
    p = load_prompt(version)
    assert p.tool_name == "record_lead"
    assert p.version == "lead_qualification@" + version.rsplit("/", 1)[1]
    assert "record_lead" in p.system
    assert "<transcript>" in p.system


@pytest.mark.parametrize("version", VERSIONS)
def test_build_request_shape(version):
    p = load_prompt(version)
    req = build_request(p, TRANSCRIPT, MODEL)
    assert "model" not in req  # the model goes in the URL
    assert req["systemInstruction"] == {"parts": [{"text": p.system}]}
    assert req["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["record_lead"]}
    }
    (tool,) = req["tools"]
    (decl,) = tool["functionDeclarations"]
    assert decl["name"] == "record_lead"
    assert decl["description"] == TOOL_DESCRIPTION
    assert decl["parameters"] == gemini_schema(p.schema)
    assert set(decl["parameters"]["properties"]) == set(p.schema["properties"])
    assert req["generationConfig"]["maxOutputTokens"] == MAX_TOKENS == 1024
    assert "$schema" in p.schema  # the loaded prompt itself is untouched
    assert req["contents"] == [
        {"role": "user", "parts": [{"text": "<transcript>\n" + TRANSCRIPT + "\n</transcript>"}]}
    ]
    json.dumps(req)  # serialisable as-is


def test_gemini_schema_conversion():
    p = load_prompt(VERSIONS[0])
    g = gemini_schema(p.schema)
    assert g["type"] == "OBJECT"
    assert "$schema" not in g and "$id" not in g and "additionalProperties" not in g
    assert g["required"] == p.schema["required"]
    contact = g["properties"]["contact"]
    assert "additionalProperties" not in contact
    assert contact["properties"]["email"] == {
        "type": "STRING", "nullable": True, "description": "Only if stated by the caller."}
    amount = g["properties"]["budget"]["properties"]["amount_usd"]
    assert amount["type"] == "NUMBER" and amount["nullable"] is True and amount["minimum"] == 0
    assert g["properties"]["buying_intent_score"] == {
        "type": "INTEGER", "minimum": 1, "maximum": 10,
        "description": "1-10 per the scoring rubric in the system prompt."}
    assert g["properties"]["timeline"]["enum"][0] == "immediate"
    assert g["properties"]["need_summary"]["maxLength"] == 300
    with pytest.raises(ValueError):
        gemini_schema({"type": ["string", "number"]})


BASE_KEYS = ["systemInstruction", "contents", "tools", "toolConfig", "generationConfig"]
NO_THINKING = {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}}


@pytest.mark.parametrize(
    "model, extra",
    [
        ("gemini-2.5-flash", NO_THINKING),
        ("gemini-2.5-flash-lite", NO_THINKING),
        ("gemini-some-future-model", {}),
    ],
)
def test_model_profile_keys_and_order(model, extra):
    req = build_request(load_prompt(VERSIONS[0]), TRANSCRIPT, model)
    assert list(req) == BASE_KEYS
    gc = req["generationConfig"]
    assert list(gc) == ["maxOutputTokens", *extra]
    assert {k: gc[k] for k in extra} == extra


def test_profile_orders_temperature_first_and_ignores_other_keys():
    params = {"m": {"thinkingConfig": {"thinkingBudget": 0}, "temperature": 0, "topK": 5}}
    assert list(model_params("m", params)) == ["temperature", "thinkingConfig"]


def test_model_params_single_source_of_truth():
    on_disk = json.loads(MODEL_PARAMS_PATH.read_text(encoding="utf-8"))
    assert MODEL_PARAMS == on_disk
    assert MODEL_PARAMS_PATH.parent.name == "prompts"  # n8n reads /prompts/model_params.json
    assert on_disk["gemini-2.5-flash"] == NO_THINKING


def test_model_params_returns_copies():
    model_params(MODEL)["thinkingConfig"]["thinkingBudget"] = 512
    assert MODEL_PARAMS[MODEL]["thinkingConfig"]["thinkingBudget"] == 0


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-2.5-flash-lite", "x"])
def test_generate_dataset_uses_same_profile(model):
    req = build_generation_request(
        {"id": "a", "category": "hot", "adversarial": None, "scenario": "s"}, model)
    gc = req["generationConfig"]
    assert gc["maxOutputTokens"] == 4000
    assert {k: gc[k] for k in gc if k != "maxOutputTokens"} == model_params(model)


def test_format_errors():
    errs = [{"path": "/buying_intent_score", "message": "must be <= 10"},
            {"path": "", "message": "must NOT have additional properties"}]
    assert format_errors(errs) == (
        "Schema validation failed:\n"
        "- /buying_intent_score: must be <= 10\n"
        "- /: must NOT have additional properties"
    )


@pytest.mark.parametrize("call_id", [None, "call_1"])
def test_build_retry_request_shape(call_id):
    first = build_request(load_prompt(VERSIONS[0]), TRANSCRIPT, MODEL)
    parts = [{"functionCall": {"name": "record_lead", "args": {"buying_intent_score": 11}},
              "thoughtSignature": "sig"}]
    errs = [{"path": "/buying_intent_score", "message": "must be <= 10"}]
    retry = build_retry_request(first, parts, "record_lead", errs, call_id)

    assert len(first["contents"]) == 1  # original not mutated
    assert {k: v for k, v in retry.items() if k != "contents"} == {
        k: v for k, v in first.items() if k != "contents"
    }
    assert retry["contents"][0] == first["contents"][0]
    assert retry["contents"][1] == {"role": "model", "parts": parts}  # signature kept
    fr = {"name": "record_lead", **({"id": call_id} if call_id else {}),
          "response": {"error": "Schema validation failed:\n- /buying_intent_score: must be <= 10"}}
    assert retry["contents"][2] == {"role": "user", "parts": [{"functionResponse": fr}]}
    assert len(retry["contents"]) == 3


def test_parse_response_blocked_prompt():
    r = parse_response({"promptFeedback": {"blockReason": "SAFETY"}}, "record_lead")
    assert r["call"] is None and r["parts"] == [] and r["finish_reason"] == "SAFETY"
    assert r["input_tokens"] is None and r["output_tokens"] is None


def test_parse_response_counts_thinking_as_output():
    r = parse_response({"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
                        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5,
                                          "thoughtsTokenCount": 7}}, "record_lead")
    assert (r["input_tokens"], r["output_tokens"]) == (10, 12)


def test_endpoint_puts_model_in_url():
    assert endpoint(MODEL) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent")


# ---------------------------------------------------- extract loop (no network)

class FakeClient:
    """Returns queued responses, recording every (model, request) it receives.

    Each queued item is a function-call args dict, or ``("text", finish)`` for
    a response with no functionCall, or ``("truncated", args)`` for a call cut
    off by MAX_TOKENS.
    """

    def __init__(self, inputs):
        self.inputs = list(inputs)
        self.requests = []
        self.models = []

    def generate(self, model, req):
        self.models.append(model)
        self.requests.append(json.loads(json.dumps(req)))
        item = self.inputs.pop(0)
        finish = "STOP"
        if isinstance(item, tuple) and item[0] == "text":
            parts, finish = [{"text": "I think this lead is..."}], item[1]
        else:
            if isinstance(item, tuple):
                finish, item = "MAX_TOKENS", item[1]
            parts = [{"functionCall": {"name": "record_lead", "args": item}}]
        return {"candidates": [{"content": {"role": "model", "parts": parts},
                                "finishReason": finish}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200}}


def test_extract_valid_first_try(valid_lead):
    client = FakeClient([valid_lead])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, MODEL)
    assert ex["valid_first_try"] and ex["valid_final"]
    assert ex["output"] == valid_lead
    assert len(client.requests) == 1 and client.models == [MODEL]
    assert ex["input_tokens"] == 1000 and ex["output_tokens"] == 200


def test_extract_retries_once_with_errors(valid_lead):
    bad = dict(valid_lead, buying_intent_score=11)
    client = FakeClient([bad, valid_lead])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, MODEL)
    assert not ex["valid_first_try"] and ex["valid_final"]
    assert len(client.requests) == 2
    retry = client.requests[1]
    assert retry["contents"][1]["parts"][0]["functionCall"]["args"] == bad
    fr = retry["contents"][2]["parts"][0]["functionResponse"]
    assert fr["name"] == "record_lead"
    assert "- /buying_intent_score: must be <= 10" in fr["response"]["error"]
    assert ex["input_tokens"] == 2000


def test_extract_gives_up_after_two_attempts(valid_lead):
    bad = dict(valid_lead, buying_intent_score=11)
    client = FakeClient([bad, bad])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, MODEL)
    assert ex["output"] is None and not ex["valid_final"]
    assert len(client.requests) == 2
    assert [a["attempt"] for a in ex["attempts"]] == [1, 2]


def test_extract_no_function_call_resends_attempt_1_unchanged(valid_lead):
    client = FakeClient([("text", "STOP"), valid_lead])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, MODEL)
    assert not ex["valid_first_try"] and ex["valid_final"]
    assert client.requests[1] == client.requests[0]
    assert ex["attempts"][0]["errors"] == [
        {"path": "/", "message": "no complete function call (finish_reason=STOP)"}
    ]


def test_extract_no_function_call_twice_fails():
    client = FakeClient([("text", "STOP"), ("text", "MALFORMED_FUNCTION_CALL")])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, MODEL)
    assert ex["output"] is None and len(client.requests) == 2
    assert ex["attempts"][1]["errors"][0]["message"].endswith(
        "(finish_reason=MALFORMED_FUNCTION_CALL)")


def test_extract_max_tokens_is_invalid_even_if_args_validate(valid_lead):
    client = FakeClient([("truncated", valid_lead), valid_lead])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, MODEL)
    assert not ex["valid_first_try"] and ex["valid_final"]
    assert len(client.requests) == 2
    fr = client.requests[1]["contents"][2]["parts"][0]["functionResponse"]
    assert fr["response"]["error"] == (
        "Schema validation failed:\n- /: no complete function call (finish_reason=MAX_TOKENS)"
    )


def test_extract_api_error_fails_without_retry():
    class Boom:
        def generate(self, model, req):
            raise GeminiAPIError(400, "bad request")

    ex = extract(Boom(), load_prompt(VERSIONS[0]), TRANSCRIPT, MODEL)
    assert ex["output"] is None and len(ex["attempts"]) == 1
    assert ex["attempts"][0]["api_error"].startswith("GeminiAPIError: 400")


def test_run_case_keeps_row_metadata_and_ignores_extra_keys(mini_dataset_path, valid_lead):
    rows = load_dataset(mini_dataset_path)
    row = dict(rows[0], notes="labeler note; must be ignored")
    client = FakeClient([valid_lead])
    case = run_eval.run_case(client, load_prompt(VERSIONS[0]), row, MODEL)
    assert case["category"] == "hot" and case["adversarial"] is None
    assert case["scores"]["route"]["correct"]
    assert "notes" not in case


def test_results_path():
    assert results_path("lead_qualification/v1", MODEL, None).name == (
        "lead_qualification_v1__gemini-2.5-flash.json"
    )
    assert results_path("lead_qualification/v2", "m", 3).name == "lead_qualification_v2__m__limit3.json"


def test_missing_api_key_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert run_eval.main(["--limit", "1"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
