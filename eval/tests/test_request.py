import json
from types import SimpleNamespace

import pytest

import run_eval
from request import (
    MAX_TOKENS,
    MODEL_PARAMS,
    MODEL_PARAMS_PATH,
    TOOL_DESCRIPTION,
    build_request,
    build_retry_request,
    format_errors,
    load_prompt,
    model_params,
)
from generate_dataset import build_generation_request
from run_eval import extract, load_dataset, results_path

VERSIONS = ["lead_qualification/v1", "lead_qualification/v2"]
TRANSCRIPT = "Agent: Hi, this is Maya.\nCaller: We need a receptionist bot."


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
    req = build_request(p, TRANSCRIPT, "claude-sonnet-5")
    assert req["model"] == "claude-sonnet-5"
    assert req["max_tokens"] == MAX_TOKENS == 1024
    assert req["system"] == p.system
    assert req["tool_choice"] == {"type": "tool", "name": "record_lead"}
    (tool,) = req["tools"]
    assert tool["name"] == "record_lead"
    assert tool["description"] == TOOL_DESCRIPTION
    assert "$schema" not in tool["input_schema"]
    assert "$id" not in tool["input_schema"]
    assert tool["input_schema"]["type"] == "object"
    assert tool["input_schema"]["properties"] == p.schema["properties"]
    assert "$schema" in p.schema  # the loaded prompt itself is untouched
    assert req["messages"] == [
        {"role": "user", "content": "<transcript>\n" + TRANSCRIPT + "\n</transcript>"}
    ]
    json.dumps(req)  # serialisable as-is


BASE_KEYS = ["system", "tools", "tool_choice", "messages"]


@pytest.mark.parametrize(
    "model, extra",
    [
        ("claude-sonnet-5", {"thinking": {"type": "disabled"}}),
        ("claude-haiku-4-5-20251001", {"temperature": 0}),
        ("claude-haiku-4-5", {"temperature": 0}),
        ("claude-some-future-model", {}),
    ],
)
def test_model_profile_keys_and_order(model, extra):
    req = build_request(load_prompt(VERSIONS[0]), TRANSCRIPT, model)
    assert list(req) == ["model", "max_tokens", *extra, *BASE_KEYS]
    assert {k: req[k] for k in extra} == extra
    assert req["max_tokens"] == 1024
    if model == "claude-sonnet-5":
        assert "temperature" not in req
    else:
        assert "thinking" not in req


def test_profile_with_both_keys_orders_temperature_first():
    params = {"m": {"thinking": {"type": "disabled"}, "temperature": 0, "top_k": 5}}
    assert list(model_params("m", params)) == ["temperature", "thinking"]  # other keys ignored


def test_model_params_single_source_of_truth():
    on_disk = json.loads(MODEL_PARAMS_PATH.read_text(encoding="utf-8"))
    assert MODEL_PARAMS == on_disk
    assert MODEL_PARAMS_PATH.parent.name == "prompts"  # n8n reads /prompts/model_params.json
    assert on_disk["claude-sonnet-5"] == {"thinking": {"type": "disabled"}}
    assert on_disk["claude-haiku-4-5-20251001"] == {"temperature": 0}


def test_model_params_returns_copies():
    model_params("claude-sonnet-5")["thinking"]["type"] = "adaptive"
    assert MODEL_PARAMS["claude-sonnet-5"]["thinking"]["type"] == "disabled"


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-haiku-4-5-20251001", "x"])
def test_generate_dataset_uses_same_profile(model):
    req = build_generation_request(
        {"id": "a", "category": "hot", "adversarial": None, "scenario": "s"}, model)
    assert {k: req[k] for k in req if k in ("temperature", "thinking")} == model_params(model)


def test_format_errors():
    errs = [{"path": "/buying_intent_score", "message": "must be <= 10"},
            {"path": "", "message": "must NOT have additional properties"}]
    assert format_errors(errs) == (
        "Schema validation failed:\n"
        "- /buying_intent_score: must be <= 10\n"
        "- /: must NOT have additional properties"
    )


def test_build_retry_request_shape():
    first = build_request(load_prompt(VERSIONS[0]), TRANSCRIPT, "claude-sonnet-5")
    content = [{"type": "tool_use", "id": "toolu_1", "name": "record_lead",
                "input": {"buying_intent_score": 11}}]
    errs = [{"path": "/buying_intent_score", "message": "must be <= 10"}]
    retry = build_retry_request(first, content, "toolu_1", errs)

    assert len(first["messages"]) == 1  # original not mutated
    assert {k: v for k, v in retry.items() if k != "messages"} == {
        k: v for k, v in first.items() if k != "messages"
    }
    assert retry["messages"][0] == first["messages"][0]
    assert retry["messages"][1] == {"role": "assistant", "content": content}
    assert retry["messages"][2] == {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "is_error": True,
            "content": "Schema validation failed:\n- /buying_intent_score: must be <= 10",
        }],
    }
    assert len(retry["messages"]) == 3


# ---------------------------------------------------- extract loop (no network)

class _Block(SimpleNamespace):
    def to_dict(self):
        return dict(vars(self))


class FakeClient:
    """Returns queued responses, recording every request it receives.

    Each queued item is a tool input dict, or ``("text", stop_reason)`` for a
    response with no tool_use block, or ``("truncated", input)`` for a
    tool_use cut off by max_tokens.
    """

    def __init__(self, inputs):
        self.inputs = list(inputs)
        self.requests = []
        self.messages = self

    def create(self, **req):
        self.requests.append(json.loads(json.dumps(req)))
        n = len(self.requests)
        item = self.inputs.pop(0)
        stop = "tool_use"
        if isinstance(item, tuple) and item[0] == "text":
            content, stop = [_Block(type="text", text="I think this lead is...")], item[1]
        else:
            if isinstance(item, tuple):
                stop, item = "max_tokens", item[1]
            content = [_Block(type="tool_use", id=f"toolu_{n}", name="record_lead", input=item)]
        return SimpleNamespace(content=content, stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


def test_extract_valid_first_try(valid_lead):
    client = FakeClient([valid_lead])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, "claude-sonnet-5")
    assert ex["valid_first_try"] and ex["valid_final"]
    assert ex["output"] == valid_lead
    assert len(client.requests) == 1
    assert ex["input_tokens"] == 1000 and ex["output_tokens"] == 200


def test_extract_retries_once_with_errors(valid_lead):
    bad = dict(valid_lead, buying_intent_score=11)
    client = FakeClient([bad, valid_lead])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, "claude-sonnet-5")
    assert not ex["valid_first_try"] and ex["valid_final"]
    assert len(client.requests) == 2
    retry = client.requests[1]
    assert retry["messages"][1]["content"][0]["id"] == "toolu_1"
    tr = retry["messages"][2]["content"][0]
    assert tr["tool_use_id"] == "toolu_1" and tr["is_error"] is True
    assert "- /buying_intent_score: must be <= 10" in tr["content"]
    assert ex["input_tokens"] == 2000


def test_extract_gives_up_after_two_attempts(valid_lead):
    bad = dict(valid_lead, buying_intent_score=11)
    client = FakeClient([bad, bad])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, "claude-sonnet-5")
    assert ex["output"] is None and not ex["valid_final"]
    assert len(client.requests) == 2
    assert [a["attempt"] for a in ex["attempts"]] == [1, 2]


def test_extract_no_tool_use_resends_attempt_1_unchanged(valid_lead):
    client = FakeClient([("text", "end_turn"), valid_lead])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, "claude-sonnet-5")
    assert not ex["valid_first_try"] and ex["valid_final"]
    assert client.requests[1] == client.requests[0]
    assert ex["attempts"][0]["errors"] == [
        {"path": "/", "message": "no complete tool_use block (stop_reason=end_turn)"}
    ]


def test_extract_no_tool_use_twice_fails():
    client = FakeClient([("text", "end_turn"), ("text", "refusal")])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, "claude-sonnet-5")
    assert ex["output"] is None and len(client.requests) == 2
    assert ex["attempts"][1]["errors"][0]["message"].endswith("(stop_reason=refusal)")


def test_extract_max_tokens_is_invalid_even_if_input_validates(valid_lead):
    client = FakeClient([("truncated", valid_lead), valid_lead])
    ex = extract(client, load_prompt(VERSIONS[0]), TRANSCRIPT, "claude-sonnet-5")
    assert not ex["valid_first_try"] and ex["valid_final"]
    assert len(client.requests) == 2
    tr = client.requests[1]["messages"][2]["content"][0]
    assert tr["tool_use_id"] == "toolu_1" and tr["is_error"] is True
    assert tr["content"] == (
        "Schema validation failed:\n- /: no complete tool_use block (stop_reason=max_tokens)"
    )


def test_run_case_keeps_row_metadata_and_ignores_extra_keys(mini_dataset_path, valid_lead):
    rows = load_dataset(mini_dataset_path)
    row = dict(rows[0], notes="labeler note; must be ignored")
    client = FakeClient([valid_lead])
    case = run_eval.run_case(client, load_prompt(VERSIONS[0]), row, "claude-sonnet-5")
    assert case["category"] == "hot" and case["adversarial"] is None
    assert case["scores"]["route"]["correct"]
    assert "notes" not in case


def test_results_path():
    assert results_path("lead_qualification/v1", "claude-sonnet-5", None).name == (
        "lead_qualification_v1__claude-sonnet-5.json"
    )
    assert results_path("lead_qualification/v2", "m", 3).name == "lead_qualification_v2__m__limit3.json"


def test_missing_api_key_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert run_eval.main(["--limit", "1"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
