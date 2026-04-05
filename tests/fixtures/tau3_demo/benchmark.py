"""Tau-bench benchmark wrapper for evo.

Runs tau-bench tasks against the agent loaded from --agent, outputs
mr-compatible JSON to stdout, and writes per-task traces to $EVO_TRACES_DIR.

Environment variables:
    TAU3_DOMAIN       tau-bench domain (default: retail)
    AGENT_MODEL       LLM model for the agent (default: gpt-5.4)
    TAU3_SPLIT        task split to run (default: train)
    TAU3_CONCURRENCY  max concurrent tasks (default: 3)
    EVO_TRACES_DIR     set by `mr run` -- directory for per-task trace files
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path


def load_agent_class(agent_path: str):
    """Dynamically load HarnessAgent from the given file path."""
    spec = importlib.util.spec_from_file_location("tau3_agent", agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {agent_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.HarnessAgent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run tau-bench benchmark")
    parser.add_argument("--agent", required=True, help="Path to agent.py")
    parser.add_argument("--task-ids", nargs="*", help="Specific task IDs to run")
    args = parser.parse_args()

    from tau2 import registry
    from tau2.data_model.simulation import TextRunConfig
    from tau2.run import run_domain

    AgentClass = load_agent_class(args.agent)

    domain = os.environ.get("TAU3_DOMAIN", "retail")
    model = os.environ.get("AGENT_MODEL", "gpt-5.4")
    split = os.environ.get("TAU3_SPLIT", "train")
    concurrency = int(os.environ.get("TAU3_CONCURRENCY", "3"))

    def create_agent(tools, domain_policy, **kwargs):
        return AgentClass(
            tools=tools,
            domain_policy=domain_policy,
            llm=kwargs.get("llm"),
            llm_args=kwargs.get("llm_args"),
        )

    if registry.get_agent_factory("tau3_bench_agent") is None:
        registry.register_agent_factory(create_agent, "tau3_bench_agent")

    config = TextRunConfig(
        domain=domain,
        agent="tau3_bench_agent",
        llm_agent=model,
        task_split_name=split,
        task_ids=args.task_ids,
        max_concurrency=concurrency,
        seed=300,
    )

    results = run_domain(config)

    traces_dir = os.environ.get("EVO_TRACES_DIR")
    if traces_dir:
        Path(traces_dir).mkdir(parents=True, exist_ok=True)

    task_scores: dict[str, float] = {}
    for sim in results.simulations:
        tid = str(sim.task_id)
        reward = float(sim.reward_info.reward) if sim.reward_info else 0.0
        task_scores[tid] = reward

        if traces_dir:
            # Build a trace with conversation history for failure analysis
            events = []
            try:
                for msg in sim.messages:
                    events.append({
                        "role": getattr(msg, "role", "unknown"),
                        "content": getattr(msg, "content", str(msg))[:2000],
                    })
            except Exception:
                events.append({"note": "could not extract message history"})

            trace = {
                "experiment_id": "tau3",
                "task_id": tid,
                "status": "passed" if reward >= 0.5 else "failed",
                "score": reward,
                "summary": f"reward={reward:.2f} domain={domain} split={split}",
                "failure_reason": None if reward >= 0.5 else "task_failed",
                "events": events,
            }
            Path(traces_dir, f"task_{tid}.json").write_text(
                json.dumps(trace, indent=2), encoding="utf-8"
            )

    score = sum(task_scores.values()) / len(task_scores) if task_scores else 0.0
    print(json.dumps({"score": round(score, 4), "tasks": task_scores}, indent=2))


if __name__ == "__main__":
    main()
