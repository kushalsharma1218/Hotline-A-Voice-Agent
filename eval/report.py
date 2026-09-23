"""Print the README results table (PRD 9.3) from every eval/results/*.json.

    uv run --project eval python eval/report.py [--include-partial]

Partial runs (made with --limit) are skipped unless --include-partial.
"""

from __future__ import annotations

import argparse
import json
import sys

from config import RESULTS_DIR
from run_eval import markdown_table, table_row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print the README eval results table.")
    ap.add_argument("--include-partial", action="store_true")
    args = ap.parse_args(argv)

    runs = []
    for path in sorted(RESULTS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("limit") and not args.include_partial:
            continue
        runs.append(data)
    if not runs:
        print(f"no results in {RESULTS_DIR}; run eval/run_eval.py first", file=sys.stderr)
        return 1

    runs.sort(key=lambda d: (d["prompt_version"], d["model"] != "claude-sonnet-5", d["model"]))
    print(markdown_table([table_row(d["prompt_version"], d["model"], d["metrics"]) for d in runs]))
    print()
    for d in runs:
        inj = d["metrics"]["injection"]
        extra = f", injection {inj['passed']}/{inj['n']}" if inj["n"] else ""
        print(f"- {d['prompt_version']} / {d['model']}: n={d['metrics']['n']}{extra}, run {d['run_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
