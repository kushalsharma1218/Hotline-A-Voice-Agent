import copy
import json

import pytest
from jsonschema import Draft202012Validator

from config import DATASET_PATH
from request import load_prompt, tool_input_schema
from validate import check_schema, validate

VERSIONS = ["lead_qualification/v1", "lead_qualification/v2"]


@pytest.fixture(params=VERSIONS)
def schema(request):
    return load_prompt(request.param).schema


@pytest.mark.parametrize("version", VERSIONS)
def test_schema_is_valid_draft_2020_12(version):
    s = load_prompt(version).schema
    Draft202012Validator.check_schema(s)  # F2.1 accept
    check_schema(s)
    check_schema(tool_input_schema(s))
    assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize("version", VERSIONS)
def test_schema_contract(version):
    s = load_prompt(version).schema

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)

    for obj in objects(s):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert set(s["properties"]) == {
        "is_sales_lead", "contact", "need_summary", "budget", "timeline", "decision_maker",
        "buying_intent_score", "intent_evidence", "follow_up_required", "call_summary",
    }


def test_v1_and_v2_schemas_have_same_structure():
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in ("description", "title")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    assert strip(load_prompt(VERSIONS[0]).schema) == strip(load_prompt(VERSIONS[1]).schema)


def test_valid_example_passes(schema, valid_lead):
    assert validate(valid_lead, schema) == []


def test_nulls_and_unknowns_pass(schema, valid_lead):
    valid_lead["contact"] = {k: None for k in valid_lead["contact"]}
    valid_lead["budget"] = {"mentioned": False, "amount_usd": None, "period": "unknown"}
    valid_lead["timeline"] = "unknown"
    valid_lead["decision_maker"] = "unknown"
    assert validate(valid_lead, schema) == []


def test_score_11_fails(schema, valid_lead):
    valid_lead["buying_intent_score"] = 11
    assert validate(valid_lead, schema) == [
        {"path": "/buying_intent_score", "message": "must be <= 10"}
    ]


def test_score_0_and_float_fail(schema, valid_lead):
    valid_lead["buying_intent_score"] = 0
    assert validate(valid_lead, schema)[0]["message"] == "must be >= 1"
    valid_lead["buying_intent_score"] = 7.5
    assert validate(valid_lead, schema) == [
        {"path": "/buying_intent_score", "message": "must be integer"}
    ]


def test_extra_field_fails(schema, valid_lead):
    valid_lead["lead_source"] = "phone"
    assert validate(valid_lead, schema) == [
        {"path": "", "message": "must NOT have additional properties"}
    ]


def test_nested_extra_field_fails(schema, valid_lead):
    valid_lead["budget"]["currency"] = "INR"
    assert validate(valid_lead, schema) == [
        {"path": "/budget", "message": "must NOT have additional properties"}
    ]


@pytest.mark.parametrize(
    "field, limit", [("need_summary", 300), ("intent_evidence", 200), ("call_summary", 500)]
)
def test_too_long_text_fails(schema, valid_lead, field, limit):
    valid_lead[field] = "x" * (limit + 1)
    assert validate(valid_lead, schema) == [
        {"path": f"/{field}", "message": f"must NOT have more than {limit} characters"}
    ]
    valid_lead[field] = "x" * limit
    assert validate(valid_lead, schema) == []


def test_missing_required_and_bad_enum(schema, valid_lead):
    del valid_lead["timeline"]
    valid_lead["decision_maker"] = "maybe"
    valid_lead["budget"]["amount_usd"] = -5
    valid_lead["contact"]["email"] = 42
    assert validate(valid_lead, schema) == [
        {"path": "", "message": "must have required property 'timeline'"},
        {"path": "/budget/amount_usd", "message": "must be >= 0"},
        {"path": "/contact/email", "message": "must be string,null"},
        {"path": "/decision_maker", "message": "must be equal to one of the allowed values"},
    ]


def test_bool_is_not_integer(schema, valid_lead):
    valid_lead["buying_intent_score"] = True
    assert validate(valid_lead, schema)[0]["path"] == "/buying_intent_score"


def test_non_object_fails(schema):
    assert validate("not json", schema) == [{"path": "", "message": "must be object"}]


@pytest.mark.skipif(not DATASET_PATH.exists(), reason="eval/dataset.jsonl not written yet")
def test_dataset_labels_validate_against_v1():
    schema = load_prompt("lead_qualification/v1").schema
    rows = [json.loads(l) for l in DATASET_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    from scoring import derive_route

    for row in rows:
        assert validate(row["expected"], schema) == [], row["id"]
        assert derive_route(row["expected"]) == row["expected_route"], row["id"]
