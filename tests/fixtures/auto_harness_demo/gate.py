from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

GATE_TASKS = [
    {
        "request": "What is the status of order 123?",
        "expected": "answer",
    },
    {
        "request": "Please refund my damaged order.",
        "expected": "approve",
    },
]


def load_agent(agent_path: Path):
    spec = importlib.util.spec_from_file_location("fixture_agent_gate", agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {agent_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()

    module = load_agent(Path(args.agent))
    for task in GATE_TASKS:
        if module.solve(task) != task["expected"]:
            sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
