"""Tau-bench benchmark wrapper for evo.

Runs tau-bench tasks against the agent loaded from --agent, outputs
evo-compatible JSON to stdout, and writes per-task traces to $EVO_TRACES_DIR
as each task completes (for live monitoring).

Environment variables:
    TAU3_DOMAIN       tau-bench domain (default: retail)
    AGENT_MODEL       LLM model for the agent (default: gpt-5.4)
    TAU3_SPLIT        task split to run (default: train)
    TAU3_CONCURRENCY  max concurrent tasks (default: 10)
    EVO_TRACES_DIR     set by `evo run` -- directory for per-task trace files
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path


def load_agent_class(agent_path: str):
    """Dynamically load EvoAgent from the given file path."""
    spec = importlib.util.spec_from_file_location("tau3_agent", agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {agent_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.EvoAgent


def _extract_events(sim) -> list[dict]:
    """Extract conversation events from a simulation, tolerating varied message types."""
    events = []
    try:
        for msg in sim.messages:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", None)
            if content is None:
                content = str(msg)
            elif not isinstance(content, str):
                try:
                    content = json.dumps(content)
                except (TypeError, ValueError):
                    content = str(content)
            events.append({"role": role, "content": content[:2000]})
    except Exception:
        events.append({"note": "could not extract remaining message history"})
    return events


def _write_trace(sim, traces_dir: str, domain: str, split: str) -> None:
    """Write a single task trace to disk immediately on completion."""
    tid = str(sim.task_id)
    reward = float(sim.reward_info.reward) if sim.reward_info else 0.0
    trace = {
        "experiment_id": "tau3",
        "task_id": tid,
        "status": "passed" if reward >= 0.5 else "failed",
        "score": reward,
        "summary": f"reward={reward:.2f} domain={domain} split={split}",
        "failure_reason": None if reward >= 0.5 else "task_failed",
        "events": _extract_events(sim),
    }
    Path(traces_dir, f"task_{tid}.json").write_text(
        json.dumps(trace, indent=2), encoding="utf-8"
    )


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
    concurrency = int(os.environ.get("TAU3_CONCURRENCY", "10"))
    traces_dir = os.environ.get("EVO_TRACES_DIR")

    if traces_dir:
        Path(traces_dir).mkdir(parents=True, exist_ok=True)

    def create_agent(tools, domain_policy, **kwargs):
        return AgentClass(
            tools=tools,
            domain_policy=domain_policy,
            llm=kwargs.get("llm"),
            llm_args=kwargs.get("llm_args"),
        )

    if registry.get_agent_factory("tau3_bench_agent") is None:
        registry.register_agent_factory(create_agent, "tau3_bench_agent")

    # Monkey-patch Results.simulations.append to write traces as tasks complete.
    # This hooks into tau2's as_completed loop without forking the runner.
    task_scores: dict[str, float] = {}
    _scores_lock = threading.Lock()

    original_append = None

    if traces_dir:
        from tau2.data_model.simulation import Results

        _original_list_append = list.append

        class TracingList(list):
            """A list that writes a trace file each time a simulation is appended."""

            def append(self, sim):
                _original_list_append(self, sim)
                tid = str(sim.task_id)
                reward = float(sim.reward_info.reward) if sim.reward_info else 0.0
                with _scores_lock:
                    task_scores[tid] = reward
                try:
                    _write_trace(sim, traces_dir, domain, split)
                    print(
                        f"  trace: task {tid} = {reward:.2f}",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    print(
                        f"  trace write failed for task {tid}: {exc}",
                        file=sys.stderr,
                    )

        # Patch Results.__init__ to use TracingList for simulations
        _orig_results_init = Results.__init__

        def _patched_init(self, *a, **kw):
            _orig_results_init(self, *a, **kw)
            # Replace the plain list with our tracing variant, preserving contents
            tracing = TracingList(self.simulations)
            object.__setattr__(self, "simulations", tracing)

        Results.__init__ = _patched_init

    config = TextRunConfig(
        domain=domain,
        agent="tau3_bench_agent",
        llm_agent=model,
        task_split_name=split,
        task_ids=args.task_ids,
        max_concurrency=concurrency,
        seed=300,
    )

    # tau-bench prints rich tables to stdout; redirect to stderr so only our
    # JSON lands on stdout (required by evo's score parser).
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        results = run_domain(config)
    finally:
        sys.stdout = real_stdout
        # Restore Results.__init__ if patched
        if traces_dir:
            Results.__init__ = _orig_results_init

    # Collect any scores not already captured by the tracing hook
    for sim in results.simulations:
        tid = str(sim.task_id)
        if tid not in task_scores:
            reward = float(sim.reward_info.reward) if sim.reward_info else 0.0
            task_scores[tid] = reward
            if traces_dir:
                _write_trace(sim, traces_dir, domain, split)

    score = sum(task_scores.values()) / len(task_scores) if task_scores else 0.0
    print(json.dumps({"score": round(score, 4), "tasks": task_scores}, indent=2))


if __name__ == "__main__":
    main()
