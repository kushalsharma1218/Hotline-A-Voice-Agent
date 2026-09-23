import copy
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def valid_lead() -> dict:
    return copy.deepcopy(json.loads((FIXTURES / "valid_lead.json").read_text(encoding="utf-8")))


@pytest.fixture
def mini_dataset_path() -> Path:
    return FIXTURES / "mini_dataset.jsonl"
