"""JSON Schema (draft 2020-12) validation with Ajv-shaped errors.

n8n validates with Ajv (``new Ajv2020({allErrors: true, strict: false})``);
this module applies the same rules with ``jsonschema`` and reports each error
the way Ajv does, so the retry feedback text and the ``validation_errors``
audit column look the same from both places:

    {"path": "/budget/amount_usd", "message": "must be >= 0"}

``path`` is Ajv's ``instancePath``: a JSON pointer to the failing value
("" for the root). For ``required`` and ``additionalProperties`` it points at
the object that is missing / has the property, as in Ajv. Messages follow
Ajv's default English messages for the keywords the lead schema uses, and
fall back to the jsonschema message for anything else.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def check_schema(schema: dict) -> None:
    """Raise ``jsonschema.SchemaError`` if ``schema`` is not valid 2020-12 (F2.1)."""
    Draft202012Validator.check_schema(schema)


def _pointer(parts) -> str:
    return "".join(
        "/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts
    )


def _ajv_messages(err: ValidationError) -> list[str]:
    kw, val = err.validator, err.validator_value
    if kw == "type":
        return [f"must be {','.join(val) if isinstance(val, list) else val}"]
    if kw == "required":
        # jsonschema yields one error per missing property, like Ajv allErrors.
        missing = [p for p in val if isinstance(err.instance, dict) and p not in err.instance]
        return [f"must have required property '{p}'" for p in missing] or [err.message]
    if kw == "additionalProperties" and val is False:
        allowed = set((err.schema or {}).get("properties", {}))
        extras = [k for k in err.instance if k not in allowed] if isinstance(err.instance, dict) else []
        # Ajv reports one error per extra property.
        return ["must NOT have additional properties"] * max(1, len(extras))
    if kw == "maximum":
        return [f"must be <= {val}"]
    if kw == "minimum":
        return [f"must be >= {val}"]
    if kw == "maxLength":
        return [f"must NOT have more than {val} characters"]
    if kw == "minLength":
        return [f"must NOT have fewer than {val} characters"]
    if kw == "enum":
        return ["must be equal to one of the allowed values"]
    return [err.message]


def validate(instance: Any, schema: dict) -> list[dict]:
    """Return a list of ``{"path", "message"}`` errors; empty list means valid."""
    validator = Draft202012Validator(schema)
    out: list[dict] = []
    for err in validator.iter_errors(instance):
        path = _pointer(err.absolute_path)
        for msg in _ajv_messages(err):
            out.append({"path": path, "message": msg})
    out.sort(key=lambda e: (e["path"], e["message"]))
    return out


def is_valid(instance: Any, schema: dict) -> bool:
    return not validate(instance, schema)
