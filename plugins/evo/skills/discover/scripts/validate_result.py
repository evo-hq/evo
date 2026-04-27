#!/usr/bin/env python3
"""Validate a benchmark result file.

Usage: python3 validate_result.py <path-to-result.json>

Exits 0 if the file exists, is non-empty, and is a JSON object with a
numeric 'score'. Exits 1 with a diagnostic on stderr otherwise.
"""

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <result.json>", file=sys.stderr)
        return 1

    path = Path(sys.argv[1])

    if not path.exists():
        print(f"FAIL: {path} does not exist", file=sys.stderr)
        return 1

    if path.stat().st_size == 0:
        print(f"FAIL: {path} is empty", file=sys.stderr)
        return 1

    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FAIL: {path} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(obj, dict):
        print(f"FAIL: expected JSON object, got {type(obj).__name__}", file=sys.stderr)
        return 1

    if "score" not in obj:
        print(f"FAIL: missing 'score' field. Keys: {list(obj.keys())}", file=sys.stderr)
        return 1

    try:
        score = float(obj["score"])
    except (TypeError, ValueError):
        print(f"FAIL: 'score' is not numeric: {obj['score']!r}", file=sys.stderr)
        return 1

    print(f"OK: {path}, score = {score}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
