#!/usr/bin/env python3
"""Validate that benchmark stdout is clean JSON with a 'score' field.

Usage:
    <benchmark_command> 2>/tmp/stderr.log | python validate_stdout.py

Exit codes:
    0 -- valid: stdout is pure JSON with a numeric 'score' field
    1 -- invalid: stdout is polluted or missing required fields

On success, prints the validated JSON to stdout (passthrough).
On failure, prints a diagnostic to stderr.
"""

import json
import sys


def main() -> int:
    raw = sys.stdin.read()

    if not raw.strip():
        print("FAIL: stdout is empty -- benchmark produced no output", file=sys.stderr)
        return 1

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Show what polluted stdout
        lines = raw.strip().splitlines()
        print("FAIL: stdout is not pure JSON. Parsing error:", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        print(f"  Total lines: {len(lines)}", file=sys.stderr)
        print(f"  First line: {lines[0][:120]}", file=sys.stderr)
        if len(lines) > 1:
            print(f"  Last line:  {lines[-1][:120]}", file=sys.stderr)
        print(
            "\nThe benchmark wrapper likely prints progress/tables to stdout.",
            file=sys.stderr,
        )
        print("Fix: redirect noisy output to stderr in the wrapper.", file=sys.stderr)
        return 1

    if not isinstance(obj, dict):
        print(f"FAIL: expected JSON object, got {type(obj).__name__}", file=sys.stderr)
        return 1

    if "score" not in obj:
        print(f"FAIL: JSON missing 'score' field. Keys: {list(obj.keys())}", file=sys.stderr)
        return 1

    try:
        score = float(obj["score"])
    except (TypeError, ValueError):
        print(f"FAIL: 'score' is not numeric: {obj['score']!r}", file=sys.stderr)
        return 1

    # Passthrough the clean JSON
    print(raw, end="")
    print(f"OK: clean JSON, score = {score}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
